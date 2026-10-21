"""
app.db.repositories.link_request_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the `link_requests` table (bot -> worker link queue).
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.link_request import LinkRequest, LinkRequestStatus


class LinkRequestRepository:
    """Data access layer for LinkRequest."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: int,
        telegram_account_id: int,
        raw_link: str,
        notify_chat_id: int | None = None,
        notify_message_id: int | None = None,
    ) -> LinkRequest:
        request = LinkRequest(
            user_id=user_id,
            telegram_account_id=telegram_account_id,
            raw_link=raw_link,
            status=LinkRequestStatus.PENDING,
            notify_chat_id=notify_chat_id,
            notify_message_id=notify_message_id,
        )
        self._session.add(request)
        await self._session.flush()
        await self._session.refresh(request)
        return request

    async def get_by_id(self, request_id: int) -> LinkRequest | None:
        result = await self._session.execute(
            select(LinkRequest).where(LinkRequest.id == request_id)
        )
        return result.scalar_one_or_none()

    async def list_pending(self, *, limit: int = 50) -> list[LinkRequest]:
        """Oldest-first, so requests are processed in submission order."""
        result = await self._session.execute(
            select(LinkRequest)
            .where(LinkRequest.status == LinkRequestStatus.PENDING)
            .order_by(LinkRequest.created_at.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def reset_processing_to_pending(self) -> int:
        """Return stale PROCESSING rows to PENDING; returns how many.

        Called once when the worker's processor starts: this processor is
        the only consumer, so any row still marked PROCESSING at startup
        belongs to a run that was stopped/crashed mid-request and would
        otherwise never be retried (list_pending only selects PENDING).
        """
        result = await self._session.execute(
            select(LinkRequest).where(
                LinkRequest.status == LinkRequestStatus.PROCESSING
            )
        )
        rows = list(result.scalars().all())
        for row in rows:
            row.status = LinkRequestStatus.PENDING
            row.error = None
        if rows:
            await self._session.flush()
        return len(rows)

    async def mark(
        self,
        request: LinkRequest,
        *,
        status: LinkRequestStatus,
        result_count: int | None = None,
        error: str | None = None,
    ) -> LinkRequest:
        request.status = status
        if result_count is not None:
            request.result_count = result_count
        if error is not None:
            request.error = error
        if status in (LinkRequestStatus.DONE, LinkRequestStatus.FAILED):
            request.processed_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self._session.refresh(request)
        return request
