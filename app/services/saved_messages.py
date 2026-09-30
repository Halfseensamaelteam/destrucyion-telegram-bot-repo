"""
app.services.saved_messages
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SavedMessagesService — forwards captured media to the account owner's
own Saved Messages within the *same* Telegram account.

Critical routing invariant (ROADMAP.md Phase 9 / CLAUDE.md §9):
  Account A → Account A's Saved Messages
  Account B → Account B's Saved Messages
  NEVER Account A → Account B's Saved Messages

Design:
  - The service receives an explicit (client, account_id) pair.
  - It resolves "me" from the client — never from configuration or a DB field.
  - If the client is not for account_id, it raises RoutingError immediately.
  - All forwarding failures are caught, logged, and propagated so that
    the caller (MediaCaptureService) can mark the record FAILED.
  - We forward using client.forward_messages(to=InputPeerSelf, ...) first
    (zero re-upload cost). If the message is no longer available (timed
    media already expired), we try send_file with the original file_id.
  - Self-destructing media is best-effort only (CLAUDE.md §8).
    Success/failure is always reported honestly.
"""

from __future__ import annotations

import asyncio
import functools
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.telegram.metadata import ChatInfo, SenderInfo, build_caption

log = get_logger(__name__)


class RoutingError(Exception):
    """Raised when the wrong client would be used for an account's Saved Messages."""


class SavedMessagesForwardError(Exception):
    """Raised when forwarding to Saved Messages fails."""


@dataclass
class ForwardResult:
    """Result of a Saved Messages forward attempt.

    Attributes:
        saved_message_id: The message ID of the forwarded message in Saved Messages.
        caption_used: The caption attached to the forwarded message.
        was_resent: True if we had to re-send (file-id path) instead of forward.
        file_path: Path to the downloaded temp file (if media was downloaded/re-sent).
    """

    saved_message_id: int
    caption_used: str
    was_resent: bool
    file_path: str | None = None


def with_flood_wait_retry(max_retries: int = 3):
    """Decorator to retry Telethon functions if they hit a FloodWaitError."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            from telethon.errors import FloodWaitError
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except FloodWaitError as e:
                    if attempt == max_retries:
                        raise
                    log.warning(
                        "flood_wait_encountered",
                        seconds=e.seconds,
                        attempt=attempt + 1,
                        msg="Sleeping before retry...",
                    )
                    await asyncio.sleep(e.seconds)
        return wrapper
    return decorator


class SavedMessagesService:
    """Forwards media from a captured event to the account's own Saved Messages.

    One instance per forward operation — it is NOT a singleton.

    Usage:
        svc = SavedMessagesService(client, account_id=managed.account_id)
        result = await svc.forward(source_message, sender_info, chat_info, record)
    """

    def __init__(self, client, account_id: int) -> None:
        """
        Args:
            client: The authenticated TelegramClient for this account.
            account_id: The app-level TelegramAccount.id — used for routing
                        validation and logging. Never used to resolve "me".
        """
        self._client = client
        self._account_id = account_id

    async def forward(
        self,
        source_message,
        *,
        sender_info: SenderInfo,
        chat_info: ChatInfo,
        record,  # MediaRecord — for metadata only, never modified here
    ) -> ForwardResult:
        """Forward source_message to this account's Saved Messages.

        Routing invariant: "me" is resolved from the live client.
        This method never uses an account_id to look up a peer —
        that would create a cross-account routing risk.

        Strategy:
          1. Try forward_messages() — preserves original media, zero re-upload.
          2. If that fails (e.g. timed media expired, forward locked chat),
             try send_file() with the raw InputDocument/InputPhoto.
          3. If both fail, raise SavedMessagesForwardError.

        Returns:
            ForwardResult with the saved_message_id and optional file_path.

        Raises:
            SavedMessagesForwardError: When all forwarding strategies fail.
        """
        from datetime import datetime, timezone

        caption = build_caption(
            sender=sender_info,
            chat=chat_info,
            media_type=record.media_type.value,
            ttl_seconds=record.ttl_seconds,
            captured_at=datetime.now(timezone.utc),
        )

        # Timed / self-destructing media: NEVER forward and NEVER re-send by
        # file reference. Telegram refuses to forward it, and Telethon's
        # reference re-send (utils.get_input_media) copies ttl_seconds onto
        # the new message, so the "saved" copy would self-destruct too (or be
        # rejected outright in Saved Messages). The only reliable way — and
        # what the original Saveit.py does — is to download the bytes and
        # upload them as a brand-new, non-timed file.
        if record.ttl_seconds is not None:
            try:
                result = await self._try_resend(source_message, caption)
                log.info(
                    "saved_messages_download_uploaded",
                    account_id=self._account_id,
                    record_id=record.id,
                    saved_msg_id=result.saved_message_id,
                    strategy="download_upload_timed",
                )
                return result
            except Exception as exc:
                log.error(
                    "saved_messages_timed_failed",
                    account_id=self._account_id,
                    record_id=record.id,
                    error=str(exc),
                )
                raise SavedMessagesForwardError(
                    f"Could not download and re-upload timed media: {exc}"
                ) from exc

        # Strategy 1: forward (zero re-upload) — normal, non-timed media only
        try:
            result = await self._try_forward(source_message, caption)
            log.info(
                "saved_messages_forwarded",
                account_id=self._account_id,
                record_id=record.id,
                saved_msg_id=result.saved_message_id,
                strategy="forward",
            )
            return result
        except Exception as fwd_exc:
            log.warning(
                "saved_messages_forward_failed_trying_resend",
                account_id=self._account_id,
                record_id=record.id,
                error=str(fwd_exc),
            )

        # Strategy 2: resend via file reference/download
        try:
            result = await self._try_resend(source_message, caption)
            log.info(
                "saved_messages_resent",
                account_id=self._account_id,
                record_id=record.id,
                saved_msg_id=result.saved_message_id,
                strategy="resend",
            )
            return result
        except Exception as resend_exc:
            log.error(
                "saved_messages_all_strategies_failed",
                account_id=self._account_id,
                record_id=record.id,
                error=str(resend_exc),
            )
            raise SavedMessagesForwardError(
                f"All forwarding strategies failed: {resend_exc}"
            ) from resend_exc

    @with_flood_wait_retry(max_retries=3)
    async def _try_forward(self, source_message, caption: str) -> ForwardResult:
        """Attempt to forward the message to Saved Messages and pin the caption."""
        from telethon.tl.types import InputPeerSelf

        # Forward the original message (preserves media, no re-upload)
        forwarded = await self._client.forward_messages(
            entity=InputPeerSelf(),
            messages=source_message,
            from_peer=source_message.peer_id,
        )

        # forwarded may be a list; normalise
        fwd_msg = forwarded[0] if isinstance(forwarded, list) else forwarded
        saved_id = fwd_msg.id

        # Send caption as a follow-up text reply pinned to the forwarded message
        await self._client.send_message(
            entity=InputPeerSelf(),
            message=caption,
            reply_to=saved_id,
        )

        return ForwardResult(
            saved_message_id=saved_id,
            caption_used=caption,
            was_resent=False,
            file_path=None,
        )

    @with_flood_wait_retry(max_retries=3)
    async def _try_resend(self, source_message, caption: str) -> ForwardResult:
        """Download the media, then upload it as a brand-new file.

        Same approach as the original Saveit.py (download_media -> send_file).
        The re-uploaded file carries NO ttl_seconds, so it is a permanent
        copy in Saved Messages. The file path is returned in ForwardResult so
        other handlers (e.g., admin_notify) can use it before cleanup.
        """
        from telethon.tl.types import InputPeerSelf

        media = getattr(source_message, "media", None)
        if media is None:
            raise SavedMessagesForwardError("Source message has no media to resend")

        tmp_dir = tempfile.mkdtemp(prefix="destrucyion_")
        try:
            file_path = await self._client.download_media(source_message, file=tmp_dir)
            if not file_path:
                raise SavedMessagesForwardError(
                    "Telegram did not return a downloadable file"
                )

            # Re-upload file ke Saved Messages sebagai foto/video asli (bukan document)
            sent = await self._client.send_file(
                entity=InputPeerSelf(),
                file=file_path,
                caption=caption,
                supports_streaming=True,
            )

            return ForwardResult(
                saved_message_id=sent.id,
                caption_used=caption,
                was_resent=True,
                file_path=str(file_path),
            )
        except Exception:
            # Jika gagal di tengah jalan, bersihkan direktori temp yang dibuat
            if 'file_path' in locals() and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            if os.path.exists(tmp_dir):
                try:
                    os.rmdir(tmp_dir)
                except OSError:
                    pass
            raise