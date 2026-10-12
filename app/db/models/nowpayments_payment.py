"""
Database model for NOWPayments crypto subscription payments.

Kept separate from the Midtrans Payment model so provider-specific statuses,
currency precision, and identifiers cannot affect the existing QRIS flow.
"""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import DateTime, ForeignKey, Index, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class NOWPaymentsPayment(Base):
    """One NOWPayments invoice/payment attempt."""

    __tablename__ = "nowpayments_payments"
    __table_args__ = (
        Index("ix_nowpayments_payments_user_status", "user_id", "payment_status"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        unique=True,
        index=True,
    )
    plan: Mapped[str] = mapped_column(String(20), nullable=False)

    # Menu prices are denominated in USD-equivalent units for stable invoice
    # pricing. Do not store crypto amounts in this field.
    price_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2),
        nullable=False,
    )
    price_currency: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="usd",
    )
    pay_currency: Mapped[str] = mapped_column(String(32), nullable=False)

    provider_payment_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        unique=True,
        index=True,
    )
    invoice_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        unique=True,
    )
    invoice_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    pay_address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    pay_amount: Mapped[Decimal | None] = mapped_column(
        Numeric(30, 12),
        nullable=True,
    )

    # Preserve the provider's lifecycle status (e.g. waiting, confirming,
    # confirmed, sending, partially_paid, finished, failed, expired).
    payment_status: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        default="waiting",
        index=True,
    )
    activated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Set once when a verified finished payment grants the plan.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
