"""
app.db.models.audit_log
~~~~~~~~~~~~~~~~~~~~~~~~
Audit log model for tracking user actions.

Phase 17: Production Hardening - Audit logs for security and compliance.
Tracks all significant user actions with timestamps and metadata.
"""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import BigInteger, DateTime, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.models.base import Base


class AuditAction(str, Enum):
    """Types of auditable actions."""

    # Account management
    ACCOUNT_CONNECTED = "account_connected"
    ACCOUNT_DISCONNECTED = "account_disconnected"
    ACCOUNT_AUTH_STARTED = "account_auth_started"
    ACCOUNT_AUTH_CODE_VERIFIED = "account_auth_code_verified"
    ACCOUNT_AUTH_2FA_VERIFIED = "account_auth_2fa_verified"

    # Subscription management
    SUBSCRIPTION_GRANTED = "subscription_granted"
    SUBSCRIPTION_REVOKED = "subscription_revoked"
    SUBSCRIPTION_RENEWED = "subscription_renewed"
    SUBSCRIPTION_EXPIRED = "subscription_expired"

    # Media operations
    MEDIA_SAVED = "media_saved"
    MEDIA_SAVE_FAILED = "media_save_failed"
    MEDIA_RETRY = "media_retry"

    # Admin operations
    ADMIN_USER_DISABLED = "admin_user_disabled"
    ADMIN_USER_ENABLED = "admin_user_enabled"
    ADMIN_API_KEY_REGENERATED = "admin_api_key_regenerated"


class AuditLog(Base):
    """Audit log entry for tracking user actions."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, index=True,
        comment="Application user ID who performed the action",
    )
    action: Mapped[str] = mapped_column(
        String(50), nullable=False, index=True,
        comment="Type of action performed (from AuditAction enum)",
    )
    telegram_account_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, index=True,
        comment="Associated Telegram account ID, if applicable",
    )
    details: Mapped[str | None] = mapped_column(
        Text, nullable=True,
        comment="Additional context/metadata as JSON string",
    )
    ip_address: Mapped[str | None] = mapped_column(
        String(45), nullable=True,
        comment="Client IP address (IPv4 or IPv6)",
    )
    user_agent: Mapped[str | None] = mapped_column(
        String(500), nullable=True,
        comment="Client user agent string",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
        comment="Timestamp when the action occurred",
    )

    def __repr__(self) -> str:
        return (
            f"<AuditLog id={self.id} user_id={self.user_id} "
            f"action={self.action} created_at={self.created_at}>"
        )
