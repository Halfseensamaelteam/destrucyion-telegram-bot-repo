"""
app.db.models.capture
~~~~~~~~~~~~~~~~~~~~~
Auto-Capture add-on (/capture) data: the paid entitlement and the single
channel/group registration it applies to.

Two tables:

  capture_subscriptions — per-user add-on entitlement. Deliberately
  mirrors `subscriptions` (same plan/status enums, same active criteria,
  same extension semantics) but is a SEPARATE product: a user needs an
  active base subscription AND an active capture add-on before protected
  (noforwards) media auto-capture is allowed for their registered
  channel. Sold only inside /capture, paid with CAP- order ids.

  capture_channels — the ONE channel/group (by Telegram chat id) whose
  protected media is auto-captured for a given connected account. The
  UNIQUE constraint on telegram_account_id enforces the product rule
  "1 channel per account" at the database level; the /capture handler
  checks first so the user gets a friendly error instead of a DB error.

Tenant isolation: capture_subscriptions is keyed by user, capture_channels
by telegram_account — a registration can never leak across accounts.
"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base
from app.db.models.subscription import SubscriptionPlan, SubscriptionStatus


class CaptureSubscription(Base):
    """One Auto-Capture add-on record per application user."""

    __tablename__ = "capture_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,           # One capture add-on per user
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
    last_order_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True,
        comment="Most recent CAP-/NPC- order id that granted/extended this add-on",
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

    user: Mapped["User"] = relationship("User")  # noqa: F821

    @property
    def is_active(self) -> bool:
        """Mirror Subscription.is_active (no lifetime plan exists here,
        but the shape is kept identical)."""
        now = datetime.now(timezone.utc)

        starts_at = self.starts_at
        if starts_at.tzinfo is None:
            starts_at = starts_at.replace(tzinfo=timezone.utc)

        if self.status != SubscriptionStatus.ACTIVE:
            return False
        if starts_at > now:
            return False

        expires_at = self.expires_at
        if expires_at is not None and expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        return expires_at is not None and expires_at > now

    def __repr__(self) -> str:
        return (
            f"<CaptureSubscription id={self.id} user_id={self.user_id} "
            f"plan={self.plan} status={self.status}>"
        )


class CaptureChannel(Base):
    """The single channel/group registered for Auto-Capture per account."""

    __tablename__ = "capture_channels"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        unique=True,           # 1 channel per account — product rule
        nullable=False,
        index=True,
    )
    chat_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False,
        comment="Telegram chat id of the registered channel/group (e.g. -100...)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    telegram_account: Mapped["TelegramAccount"] = relationship("TelegramAccount")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<CaptureChannel id={self.id} "
            f"account_id={self.telegram_account_id} chat_id={self.chat_id}>"
        )
