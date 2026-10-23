"""
app.db.repositories.capture_subscription_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the `capture_subscriptions` table.

Mirrors SubscriptionRepository exactly (same signatures and active
criteria) — the Auto-Capture add-on is a separate product from the base
subscription, but entitlement semantics are identical: PostgreSQL is the
source of truth, never in-memory state.
"""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.capture import CaptureSubscription
from app.db.models.subscription import SubscriptionPlan, SubscriptionStatus


class CaptureSubscriptionRepository:
    """Data access layer for CaptureSubscription records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_user_id(self, user_id: int) -> CaptureSubscription | None:
        result = await self._session.execute(
            select(CaptureSubscription).where(
                CaptureSubscription.user_id == user_id
            )
        )
        return result.scalar_one_or_none()

    async def is_active(self, user_id: int) -> bool:
        """Return True if the user has a currently active capture add-on.

        Active criteria mirror SubscriptionRepository.is_active:
            status == ACTIVE AND starts_at <= now AND expires_at > now.
        """
        capture_sub = await self.get_by_user_id(user_id)
        if capture_sub is None:
            return False
        return capture_sub.is_active

    async def create(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
        starts_at: datetime,
        expires_at: datetime | None = None,
        status: SubscriptionStatus = SubscriptionStatus.ACTIVE,
        last_order_id: str | None = None,
    ) -> CaptureSubscription:
        capture_sub = CaptureSubscription(
            user_id=user_id,
            plan=plan,
            starts_at=starts_at,
            expires_at=expires_at,
            status=status,
            last_order_id=last_order_id,
        )
        self._session.add(capture_sub)
        await self._session.flush()
        await self._session.refresh(capture_sub)
        return capture_sub

    async def renew(
        self,
        capture_sub: CaptureSubscription,
        *,
        new_expires_at: datetime,
        plan: SubscriptionPlan | None = None,
        last_order_id: str | None = None,
    ) -> CaptureSubscription:
        """Extend or renew an existing capture add-on."""
        capture_sub.expires_at = new_expires_at
        capture_sub.status = SubscriptionStatus.ACTIVE
        if plan is not None:
            capture_sub.plan = plan
        if last_order_id is not None:
            capture_sub.last_order_id = last_order_id
        capture_sub.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self._session.refresh(capture_sub)
        return capture_sub

    async def cancel(
        self, capture_sub: CaptureSubscription
    ) -> CaptureSubscription:
        """Mark a capture add-on as cancelled."""
        capture_sub.status = SubscriptionStatus.CANCELLED
        capture_sub.updated_at = datetime.now(timezone.utc)
        await self._session.flush()
        await self._session.refresh(capture_sub)
        return capture_sub
