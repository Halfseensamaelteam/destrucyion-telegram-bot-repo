"""
app.db.repositories.link_request_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the `link_requests` table (bot -> worker link queue).
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
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

    async def claim_pending(self, *, limit: int) -> list[LinkRequest]:
        """Atomically claim up to ``limit`` PENDING rows (-> PROCESSING).

        A single UPDATE ... WHERE status = PENDING AND id IN (SELECT ...
        ORDER BY created_at LIMIT n) RETURNING: under PostgreSQL's READ
        COMMITTED, rows locked by a concurrent claim are re-checked
        against the status predicate after the lock wait, so two worker
        processes can never claim the same row. This is what makes
        running several worker containers against one queue safe.

        Claiming stamps ``processed_at`` as the claim time — that is the
        clock reset_processing_to_pending(older_than_seconds=...) uses
        to tell a crashed worker's rows apart from a live worker's.
        The caller commits; claimed rows come back oldest-first.
        """
        subquery = (
            select(LinkRequest.id)
            .where(LinkRequest.status == LinkRequestStatus.PENDING)
            .order_by(LinkRequest.created_at.asc())
            .limit(limit)
        )
        statement = (
            update(LinkRequest)
            .where(
                LinkRequest.status == LinkRequestStatus.PENDING,
                LinkRequest.id.in_(subquery),
            )
            .values(
                status=LinkRequestStatus.PROCESSING,
                processed_at=datetime.now(timezone.utc),
            )
            .returning(LinkRequest)
        )
        result = await self._session.execute(statement)
        rows = list(result.scalars().all())
        rows.sort(key=lambda row: row.created_at)
        return rows

    async def reset_processing_to_pending(
        self, *, older_than_seconds: float | None = None
    ) -> int:
        """Return stale PROCESSING rows to PENDING; returns how many.

        Without a cutoff this resets every PROCESSING row — only safe
        when this processor is the sole consumer (its original use at
        startup). With several workers running, pass
        ``older_than_seconds``: only rows claimed longer ago than that
        are reset, so a live worker's in-flight requests are never
        stolen, while a crashed worker's rows become retryable again.
        Age is measured from processed_at (claim time), falling back to
        created_at.
        """
        result = await self._session.execute(
            select(LinkRequest).where(
                LinkRequest.status == LinkRequestStatus.PROCESSING
            )
        )
        rows = list(result.scalars().all())
        if older_than_seconds is not None:
            cutoff = datetime.now(timezone.utc) - timedelta(
                seconds=older_than_seconds
            )

            def _age_stamp(row: LinkRequest) -> datetime:
                stamp = row.processed_at or row.created_at
                if stamp.tzinfo is None:  # SQLite round-trips naive values
                    stamp = stamp.replace(tzinfo=timezone.utc)
                return stamp

            rows = [row for row in rows if _age_stamp(row) < cutoff]
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
