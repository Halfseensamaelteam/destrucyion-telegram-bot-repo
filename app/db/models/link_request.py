"""
app.db.models.link_request
~~~~~~~~~~~~~~~~~~~~~~~~~~
A user-submitted Telegram post/invite link waiting for the worker.

Why a table (and not a direct bot -> worker call): the bot process and the
worker process are separate, and only the worker holds Telethon clients.
The bot therefore records the user's intent in PostgreSQL — the project's
authoritative store (AGENTS.md rule 6) — and the worker's
LinkRequestProcessor drains PENDING rows with the owner's own client,
exactly like TelegramClientManager.sync_accounts reconciles accounts
from the DB. No Redis queue, no in-memory state is the source of truth.

Tenant isolation: a row carries BOTH user_id and telegram_account_id, and
the processor only ever uses the client belonging to that account row.
"""

import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.models.base import Base


class LinkRequestStatus(str, enum.Enum):
    """Lifecycle of a link request."""

    PENDING = "pending"          # Written by the bot, waiting for the worker
    PROCESSING = "processing"    # Worker picked it up
    DONE = "done"                # Saved (see result_count)
    FAILED = "failed"            # See error


class LinkRequest(Base):
    """One submitted link (a single post, a range, or an invite)."""

    __tablename__ = "link_requests"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    telegram_account_id: Mapped[int] = mapped_column(
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    raw_link: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[LinkRequestStatus] = mapped_column(
        Enum(
            LinkRequestStatus,
            name="linkrequeststatus",
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        default=LinkRequestStatus.PENDING,
        nullable=False,
        index=True,
    )
    result_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # The bot's single status message for this request. The bot sends
    # "Queued" first and stores its ids here; the worker then EDITS that
    # same message (Working -> Done/Failed) instead of spamming the chat
    # with a new message per state — the reference bot's pattern.
    notify_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    notify_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    processed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    user: Mapped["User"] = relationship("User")  # noqa: F821
    telegram_account: Mapped["TelegramAccount"] = relationship("TelegramAccount")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"<LinkRequest id={self.id} account_id={self.telegram_account_id} "
            f"status={self.status}>"
        )
