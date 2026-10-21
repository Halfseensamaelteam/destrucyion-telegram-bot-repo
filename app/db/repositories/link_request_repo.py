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
        self, *, user_id: int, telegram_account_id: int, raw_link: str
    ) -> LinkRequest:
        request = LinkRequest(
            user_id=user_id,
            telegram_account_id=telegram_account_id,
            raw_link=raw_link,
            status=LinkRequestStatus.PENDING,
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
