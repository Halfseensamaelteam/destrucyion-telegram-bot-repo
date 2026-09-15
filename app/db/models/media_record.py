"""
app.db.models.media_record
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Records every media item seen by a Telegram account.

The UNIQUE constraint on (telegram_account_id, source_chat_id, source_message_id)
enforces idempotency at the database level. Never use a Python set() for this.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    BigInteger,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class MediaType(str, enum.Enum):
    """Supported Telegram media types."""

    PHOTO = "photo"
    VIDEO = "video"
    DOCUMENT = "document"
    VOICE = "voice"
    VIDEO_NOTE = "video_note"
    STICKER = "sticker"
    UNKNOWN = "unknown"


class MediaRecordStatus(str, enum.Enum):
    """Processing status of a media item."""

    PENDING = "pending"      # Queued, not yet saved
    SAVED = "saved"          # Successfully forwarded to Saved Messages
    FAILED = "failed"        # Processing failed; see error field
    SKIPPED = "skipped"      # Intentionally skipped (e.g. unsupported type)


class MediaRecord(Base):
    """Tracks every media item seen/saved for a Telegram account."""

    __tablename__ = "media_records"
    __table_args__ = (
        # Idempotency constraint — CLAUDE.md §10 & AGENTS.md rule 7
        UniqueConstraint(
            "telegram_account_id",
            "source_chat_id",
            "source_message_id",
            name="uq_media_record_idempotency",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Source context
    source_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_chat_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    source_chat_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_message_id: Mapped[int] = mapped_column(BigInteger, nullable=False)

    # Sender metadata
    sender_telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sender_username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_display_name: Mapped[str | None] = mapped_column(String(512), nullable=True)

    # Media metadata
    media_type: Mapped[MediaType] = mapped_column(
        Enum(
            MediaType,
            name="mediatype",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
    )
    ttl_seconds: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
        comment="TTL from Telegram for self-destructing media. NULL = not timed.",
    )

    # Result
    saved_message_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True,
        comment="Message ID in the account's Saved Messages after forwarding",
    )
    status: Mapped[MediaRecordStatus] = mapped_column(
        Enum(
            MediaRecordStatus,
            name="mediarecordstatus",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=MediaRecordStatus.PENDING,
        nullable=False,
        index=True,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    saved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="media_records")  # noqa: F821
    telegram_account: Mapped["TelegramAccount"] = relationship(  # noqa: F821
        "TelegramAccount", back_populates="media_records"
    )

    def __repr__(self) -> str:
        return (
            f"<MediaRecord id={self.id} account_id={self.telegram_account_id} "
            f"msg={self.source_message_id} status={self.status}>"
        )
