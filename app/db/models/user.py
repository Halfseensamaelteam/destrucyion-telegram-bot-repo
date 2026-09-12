"""
app.db.models.user
~~~~~~~~~~~~~~~~~~
Application user model.

One application user may own multiple Telegram accounts and one subscription.
"""

from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class User(Base):
    """An application user who interacts through the Telegram Bot."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False, index=True,
        comment="Telegram user ID — the Bot user's ID from Update.from_user",
    )
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    first_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # API Key — stored as SHA-256 hex digest. The raw key is NEVER stored.
    api_key_hash: Mapped[str | None] = mapped_column(
        String(64), unique=True, nullable=True, index=True,
        comment="SHA-256 hex digest of the user's API key. Raw key is never stored.",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    # Relationships
    telegram_accounts: Mapped[list["TelegramAccount"]] = relationship(  # noqa: F821
        "TelegramAccount", back_populates="user", cascade="all, delete-orphan"
    )
    subscription: Mapped["Subscription | None"] = relationship(  # noqa: F821
        "Subscription", back_populates="user", uselist=False, cascade="all, delete-orphan"
    )
    media_records: Mapped[list["MediaRecord"]] = relationship(  # noqa: F821
        "MediaRecord", back_populates="user", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} tg_id={self.telegram_user_id}>"
