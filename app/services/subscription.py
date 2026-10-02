"""
app.services.subscription
~~~~~~~~~~~~~~~~~~~~~~~~~
Business logic for managing user subscriptions.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Subscription, SubscriptionPlan, SubscriptionStatus, User
from app.db.repositories import SubscriptionRepository


class SubscriptionError(Exception):
    """Base exception for subscription-related errors."""
    pass


class SubscriptionService:
    """Service layer for subscription operations."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = SubscriptionRepository(session)

    def _calculate_expiration(
        self, plan: SubscriptionPlan, start_date: datetime
    ) -> datetime | None:
        """Calculate the expiration date based on the plan type."""
        if plan == SubscriptionPlan.WEEKLY:
            return start_date + timedelta(days=7)
        if plan == SubscriptionPlan.MONTHLY:
            return start_date + timedelta(days=30)
        if plan == SubscriptionPlan.LIFETIME:
            return None
        raise ValueError(f"Unknown subscription plan: {plan}")

    async def get_subscription(self, user_id: int) -> Subscription | None:
        """Fetch a user's subscription record."""
        return await self._repo.get_by_user_id(user_id)

    async def check_active(self, user_id: int) -> bool:
        """Return True if the user has a currently active subscription."""
        return await self._repo.is_active(user_id)

    async def grant_subscription(
        self,
        user_id: int,
        plan: SubscriptionPlan,
        starts_at: datetime | None = None,
    ) -> Subscription:
        """Grant or extend a subscription for a user.

        If an existing subscription is currently active, a new paid plan
        extends from the existing expiry date instead of replacing it.

        Expired or cancelled subscriptions restart from the current time.
        Lifetime subscriptions have no expiry.
        """
        now = datetime.now(timezone.utc)
        requested_start = starts_at or now

        existing_sub = await self._repo.get_by_user_id(user_id)

        if existing_sub is not None:
            if plan == SubscriptionPlan.LIFETIME:
                return await self._repo.renew(
                    existing_sub,
                    new_expires_at=None,
                    plan=plan,
                )

            existing_expires_at = existing_sub.expires_at

            if existing_expires_at is not None and existing_expires_at.tzinfo is None:
                existing_expires_at = existing_expires_at.replace(
                    tzinfo=timezone.utc
                )

            if existing_sub.is_active and existing_expires_at is not None:
                new_expires_at = self._calculate_expiration(
                    plan,
                    existing_expires_at,
                )
            else:
                new_expires_at = self._calculate_expiration(
                    plan,
                    requested_start,
                )

            existing_sub.starts_at = (
                existing_sub.starts_at
                if existing_sub.is_active
                else requested_start
            )

            return await self._repo.renew(
                existing_sub,
                new_expires_at=new_expires_at,
                plan=plan,
            )

        expires_at = self._calculate_expiration(plan, requested_start)

        return await self._repo.create(
            user_id=user_id,
            plan=plan,
            starts_at=requested_start,
            expires_at=expires_at,
            status=SubscriptionStatus.ACTIVE,
        )

    async def renew_subscription(
        self,
        user_id: int,
        plan: SubscriptionPlan | None = None,
    ) -> Subscription:
        """Renew an existing subscription."""

        sub = await self._repo.get_by_user_id(user_id)
        if sub is None:
            raise SubscriptionError(
                "Cannot renew: user has no subscription record. "
                "Use grant_subscription first."
            )

        now = datetime.now(timezone.utc)
        current_plan = plan or sub.plan

        if current_plan == SubscriptionPlan.LIFETIME:
            return await self._repo.renew(
                sub,
                new_expires_at=None,
                plan=current_plan,
            )

        expires_at = sub.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if sub.is_active and expires_at and expires_at > now:
            new_expires = self._calculate_expiration(
                current_plan,
                expires_at,
            )
        else:
            new_expires = self._calculate_expiration(
                current_plan,
                now,
            )

        return await self._repo.renew(
            sub,
            new_expires_at=new_expires,
            plan=current_plan,
        )

    async def revoke_subscription(self, user_id: int) -> Subscription:
        """Revoke/cancel an active subscription immediately."""

        sub = await self._repo.get_by_user_id(user_id)
        if sub is None:
            raise SubscriptionError(
                "User has no subscription to revoke."
            )

        return await self._repo.cancel(sub)
