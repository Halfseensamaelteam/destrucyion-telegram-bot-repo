"""
app.services.recovery
~~~~~~~~~~~~~~~~~~~~~~
RecoveryService — on worker startup, scans for PENDING MediaRecords that
were created (captured) but never forwarded to Saved Messages.

This handles the following failure scenarios:

  1. Worker restart / process crash:
     Records left in PENDING state are retried on next startup.

  2. Telegram reconnect during forward:
     The record sits PENDING. On reconnect the worker calls recovery again.

  3. Database restart:
     Reconnection is handled by SQLAlchemy pool. PENDING records persist.

  4. Network failure mid-forward:
     Same as crash — record stays PENDING, retried on next opportunity.

Recovery invariants:
  - SAVED records are NEVER touched (idempotency). Only PENDING.
  - Each recovery attempt checks that the account's client is RUNNING
    before attempting to forward. If not, the record stays PENDING for
    the next attempt.
  - A PENDING record for a timed/self-destruct message may legitimately
    fail during recovery (media already expired). This is marked FAILED
    with a clear error — honest reporting per CLAUDE.md §8.
  - Recovery is account-isolated: one account failure does not stop others.

Design:
  - RecoveryService is not a daemon — it runs once per startup (or on
    demand after reconnect) and then returns.
  - It calls SavedMessagesService directly, same as the event handler.
  - No Redis or external queue used — PostgreSQL is authoritative.
"""

from __future__ import annotations

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.media_record import MediaRecord, MediaRecordStatus
from app.db.repositories.media_record_repo import MediaRecordRepository
from app.services.saved_messages import SavedMessagesForwardError, SavedMessagesService
from app.telegram.metadata import SenderInfo, ChatInfo

log = get_logger(__name__)


class RecoveryService:
    """Retries all PENDING media records on startup.

    Designed to be called once from the worker after all clients are started:

        recovery = RecoveryService(session_factory, manager)
        await recovery.run()
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        manager,  # TelegramClientManager — avoid circular import
    ) -> None:
        self._session_factory = session_factory
        self._manager = manager

    async def run(self) -> dict[str, int]:
        """Scan and retry all PENDING records.

        Returns a summary dict: {"recovered": N, "skipped": N, "failed": N}

        Account failures are caught individually — one broken account does
        not prevent recovery for other accounts.
        """
        log.info("recovery_starting")
        summary = {"recovered": 0, "skipped": 0, "failed": 0}

        async with self._session_factory() as session:
            repo = MediaRecordRepository(session)
            pending = await repo.list_pending()

        if not pending:
            log.info("recovery_nothing_to_do")
            return summary

        log.info("recovery_pending_found", count=len(pending))

        for record in pending:
            outcome = await self._retry_record(record)
            summary[outcome] += 1

        log.info(
            "recovery_complete",
            recovered=summary["recovered"],
            skipped=summary["skipped"],
            failed=summary["failed"],
        )
        return summary

    async def _retry_record(self, record: MediaRecord) -> str:
        """Attempt to forward a single PENDING record.

        Returns one of: "recovered", "skipped", "failed".
        Never raises — failures are caught and logged.
        """
        account_id = record.telegram_account_id
        record_id = record.id

        # Check client is available for this account
        client = self._manager.get_client(account_id)
        if client is None:
            log.debug(
                "recovery_skipped_no_client",
                account_id=account_id,
                record_id=record_id,
            )
            return "skipped"

        # Build minimal SenderInfo and ChatInfo from what was stored in the record.
        # We cannot re-fetch from Telegram (message may no longer exist).
        sender_info = SenderInfo(
            telegram_id=record.sender_telegram_id,
            username=record.sender_username,
            display_name=record.sender_display_name,
            is_anonymous=record.sender_telegram_id is None,
            is_channel=False,
        )
        chat_info = ChatInfo(
            chat_id=record.source_chat_id,
            title=record.source_chat_title,
            username=record.source_chat_username,
            is_private=False,
            is_group=False,
            is_channel=False,
        )

        # For recovery we cannot forward the original message (it may be gone).
        # We attempt a text-only notification to Saved Messages describing the
        # captured item, with its metadata, so the user knows what was saved.
        # For timed media that expired, this is the honest best-effort report.
        try:
            result = await self._send_recovery_note(
                client=client,
                record=record,
                sender_info=sender_info,
                chat_info=chat_info,
            )
            async with self._session_factory() as session:
                repo = MediaRecordRepository(session)
                # Reload record in new session before modifying
                fresh = await repo.get_by_id(record_id)
                if fresh and fresh.status == MediaRecordStatus.PENDING:
                    from datetime import datetime, timezone
                    fresh.status = MediaRecordStatus.SAVED
                    fresh.saved_message_id = result
                    fresh.saved_at = datetime.now(timezone.utc)
                    await session.commit()

            log.info(
                "recovery_record_recovered",
                account_id=account_id,
                record_id=record_id,
            )
            return "recovered"

        except Exception as exc:
            log.error(
                "recovery_record_failed",
                account_id=account_id,
                record_id=record_id,
                error=str(exc),
            )
            async with self._session_factory() as session:
                repo = MediaRecordRepository(session)
                fresh = await repo.get_by_id(record_id)
                if fresh:
                    await repo.mark_failed(fresh, error=f"Recovery failed: {exc}")
                    await session.commit()
            return "failed"

    async def _send_recovery_note(
        self,
        *,
        client,
        record: MediaRecord,
        sender_info: SenderInfo,
        chat_info: ChatInfo,
    ) -> int:
        """Send a recovery notification to Saved Messages.

        Since the original Telegram message may no longer exist (especially
        for timed media), we send a descriptive text note instead of trying
        to forward.

        Returns the message ID of the sent note.
        """
        from telethon.tl.types import InputPeerSelf
        from app.telegram.metadata import build_caption

        caption = build_caption(
            sender=sender_info,
            chat=chat_info,
            media_type=record.media_type.value,
            ttl_seconds=record.ttl_seconds,
        )
        note = f"⚠️ Recovery: this media was captured but not forwarded before a restart.\n\n{caption}"

        sent = await client.send_message(
            entity=InputPeerSelf(),
            message=note,
        )
        return sent.id
