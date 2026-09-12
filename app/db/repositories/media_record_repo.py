"""
app.db.repositories.media_record_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the `media_records` table.

Idempotency is enforced by the database UNIQUE constraint on
(telegram_account_id, source_chat_id, source_message_id).

Never use a Python set() for deduplication — AGENTS.md rule 7.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.media_record import MediaRecord, MediaRecordStatus, MediaType


class MediaRecordRepository:
    """Data access layer for MediaRecord."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, record_id: int) -> MediaRecord | None:
        result = await self._session.execute(
            select(MediaRecord).where(MediaRecord.id == record_id)
        )
        return result.scalar_one_or_none()

    async def list_by_account(
        self,
        telegram_account_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MediaRecord]:
        result = await self._session.execute(
            select(MediaRecord)
            .where(MediaRecord.telegram_account_id == telegram_account_id)
            .order_by(MediaRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_by_user(
        self,
        user_id: int,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MediaRecord]:
        result = await self._session.execute(
            select(MediaRecord)
            .where(MediaRecord.user_id == user_id)
            .order_by(MediaRecord.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_by_source(
        self,
        telegram_account_id: int,
        source_chat_id: int,
        source_message_id: int,
    ) -> MediaRecord | None:
        """Look up an existing record by idempotency key."""
        result = await self._session.execute(
            select(MediaRecord).where(
                MediaRecord.telegram_account_id == telegram_account_id,
                MediaRecord.source_chat_id == source_chat_id,
                MediaRecord.source_message_id == source_message_id,
            )
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        user_id: int,
        telegram_account_id: int,
        source_chat_id: int,
        source_message_id: int,
        media_type: MediaType,
        source_chat_title: str | None = None,
        source_chat_username: str | None = None,
        sender_telegram_id: int | None = None,
        sender_username: str | None = None,
        sender_display_name: str | None = None,
        ttl_seconds: int | None = None,
        status: MediaRecordStatus = MediaRecordStatus.PENDING,
    ) -> MediaRecord:
        record = MediaRecord(
            user_id=user_id,
            telegram_account_id=telegram_account_id,
            source_chat_id=source_chat_id,
            source_chat_title=source_chat_title,
            source_chat_username=source_chat_username,
            source_message_id=source_message_id,
            sender_telegram_id=sender_telegram_id,
            sender_username=sender_username,
            sender_display_name=sender_display_name,
            media_type=media_type,
            ttl_seconds=ttl_seconds,
            status=status,
        )
        self._session.add(record)
        await self._session.flush()
        await self._session.refresh(record)
        return record

    async def create_or_skip(
        self,
        *,
        user_id: int,
        telegram_account_id: int,
        source_chat_id: int,
        source_message_id: int,
        media_type: MediaType,
        **kwargs: object,
    ) -> tuple[MediaRecord, bool]:
        """Create a MediaRecord or return the existing one (idempotent).

        Returns (record, created) where created=False means a duplicate was skipped.
        The deduplication is authoritative at the database level via UNIQUE constraint.
        """
        existing = await self.get_by_source(
            telegram_account_id, source_chat_id, source_message_id
        )
        if existing is not None:
            return existing, False

        record = await self.create(
            user_id=user_id,
            telegram_account_id=telegram_account_id,
            source_chat_id=source_chat_id,
            source_message_id=source_message_id,
            media_type=media_type,
            **kwargs,
        )
        return record, True

    async def mark_saved(
        self,
        record: MediaRecord,
        *,
        saved_message_id: int,
        saved_at: datetime,
    ) -> MediaRecord:
        record.status = MediaRecordStatus.SAVED
        record.saved_message_id = saved_message_id
        record.saved_at = saved_at
        await self._session.flush()
        await self._session.refresh(record)
        return record

    async def mark_failed(self, record: MediaRecord, *, error: str) -> MediaRecord:
        record.status = MediaRecordStatus.FAILED
        record.error = error
        await self._session.flush()
        await self._session.refresh(record)
        return record

    async def list_pending(
        self,
        telegram_account_id: int | None = None,
        *,
        limit: int = 500,
    ) -> list[MediaRecord]:
        """Return all PENDING records, optionally filtered to one account.

        Used by RecoveryService on worker startup to retry any records that
        were created but not forwarded before a crash/restart.

        Results are ordered oldest-first so we process in arrival order.
        """
        q = (
            select(MediaRecord)
            .where(MediaRecord.status == MediaRecordStatus.PENDING)
            .order_by(MediaRecord.created_at.asc())
            .limit(limit)
        )
        if telegram_account_id is not None:
            q = q.where(MediaRecord.telegram_account_id == telegram_account_id)
        result = await self._session.execute(q)
        return list(result.scalars().all())

    async def reset_to_pending(self, record: MediaRecord) -> MediaRecord:
        """Reset a FAILED record back to PENDING so it can be retried.

        Only used by RecoveryService — never called on SAVED records.
        """
        record.status = MediaRecordStatus.PENDING
        record.error = None
        await self._session.flush()
        await self._session.refresh(record)
        return record
