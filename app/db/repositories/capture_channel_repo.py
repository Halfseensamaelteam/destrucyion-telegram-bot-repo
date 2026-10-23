"""
app.db.repositories.capture_channel_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Data access for CaptureChannel — the one registered channel per account.
"""

from __future__ import annotations

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.capture import CaptureChannel


class CaptureChannelRepository:
    """Repository for Auto-Capture channel registrations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_account_id(
        self, telegram_account_id: int
    ) -> CaptureChannel | None:
        result = await self._session.execute(
            select(CaptureChannel).where(
                CaptureChannel.telegram_account_id == telegram_account_id
            )
        )
        return result.scalar_one_or_none()

    async def create(self, registration: CaptureChannel) -> CaptureChannel:
        self._session.add(registration)
        await self._session.flush()
        return registration

    async def delete_for_account(self, telegram_account_id: int) -> bool:
        result = await self._session.execute(
            delete(CaptureChannel).where(
                CaptureChannel.telegram_account_id == telegram_account_id
            )
        )
        await self._session.flush()
        return (result.rowcount or 0) > 0
