"""
app.db.models.subscription
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Per-user subscription record.

Subscription is the database-authoritative source of truth.
Never use in-memory timers or Redis as the source of truth for subscription state.

Active subscription criteria (CLAUDE.md §11):
    status == ACTIVE
    AND starts_at <= now
    AND expires_at > now
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class SubscriptionPlan(str, enum.Enum):
    """Available subscription plans."""

    WEEKLY = "weekly"
    MONTHLY = "monthly"
    LIFETIME = "lifetime"


class SubscriptionStatus(str, enum.Enum):
    """Lifecycle state of a subscription."""

    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Subscription(Base):
    """One subscription record per application user."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,           # One subscription per user
        nullable=False,
        index=True,
    )
    plan: Mapped[SubscriptionPlan] = mapped_column(
        Enum(
            SubscriptionPlan,
            name="subscriptionplan",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    starts_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
        comment="NULL for lifetime plan",
    )
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(
            SubscriptionStatus,
            name="subscriptionstatus",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=SubscriptionStatus.ACTIVE,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="subscription")  # noqa: F821

    @property
    def is_active(self) -> bool:
        """Return True if the subscription is currently active.

        Uses Python-side evaluation — prefer the repo method for DB queries.
        """
        now = datetime.now(timezone.utc)
        
        starts_at = self.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)
            
        if self.status != SubscriptionStatus.ACTIVE:
            return False
        if starts_at > now:
            return False
        if self.plan == SubscriptionPlan.LIFETIME:
            return True
            
        expires_at = self.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
            
        return expires_at is not None and expires_at > now

    def __repr__(self) -> str:
        return (
            f"<Subscription id={self.id} user_id={self.user_id} "
            f"plan={self.plan} status={self.status}>"
        )
