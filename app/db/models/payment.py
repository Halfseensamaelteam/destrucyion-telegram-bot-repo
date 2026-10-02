"""
app.db.models.payment
~~~~~~~~~~~~~~~~~~~~~
Database model for Midtrans payment transactions.
"""

import enum
from datetime import datetime

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class PaymentStatus(str, enum.Enum):
    """Payment transaction status."""

    PENDING = "pending"
    SETTLEMENT = "settlement"
    EXPIRE = "expire"
    CANCEL = "cancel"
    DENY = "deny"


class Payment(Base):
    """Store Midtrans payment transactions."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(
        primary_key=True,
        autoincrement=True,
    )

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

    transaction_id: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
        index=True,
    )

    plan: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
    )

    amount: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )

    currency: Mapped[str] = mapped_column(
        String(3),
        nullable=False,
        default="IDR",
    )

    status: Mapped[PaymentStatus] = mapped_column(
        Enum(
            PaymentStatus,
            name="payment_status",
        ),
        nullable=False,
        default=PaymentStatus.PENDING,
    )

    qr_string: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
    )

    expiry_time: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )