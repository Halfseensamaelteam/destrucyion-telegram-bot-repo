"""
app.db.models.telegram_account
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
One Telegram user account owned by an application user.

Each account has its own isolated Telethon client and encrypted session string.
Never share sessions between accounts or users.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class TelegramAccountStatus(str, enum.Enum):
    """Lifecycle state of a Telegram account connection."""

    ACTIVE = "active"          # Client is running and connected
    PAUSED = "paused"          # Deliberately paused (e.g. subscription expired)
    ERROR = "error"            # Connection failed; see last_error
    DISCONNECTED = "disconnected"  # Explicitly disconnected by user


class TelegramAccount(Base):
    """A Telegram user account linked to an application user."""

    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_user_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True,
        comment="Filled in after successful Telegram login",
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    phone_masked: Mapped[str | None] = mapped_column(
        String(50), nullable=True,
        comment="Masked phone like +62***1234 — NEVER store full phone",
    )
    session_ciphertext: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Fernet-encrypted Telethon StringSession — NEVER log or expose",
    )
    status: Mapped[TelegramAccountStatus] = mapped_column(
        Enum(
            TelegramAccountStatus,
            name="telegramaccountstatus",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=TelegramAccountStatus.DISCONNECTED,
        nullable=False,
    )
    last_connected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
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
    user: Mapped["User"] = relationship("User", back_populates="telegram_accounts")  # noqa: F821
    media_records: Mapped[list["MediaRecord"]] = relationship(  # noqa: F821
        "MediaRecord", back_populates="telegram_account", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<TelegramAccount id={self.id} user_id={self.user_id} "
            f"status={self.status}>"
        )
