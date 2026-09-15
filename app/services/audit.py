"""
app.services.audit
~~~~~~~~~~~~~~~~~~
Audit logging service.

Phase 17: Production Hardening - Centralized audit logging for security and compliance.
Provides a simple interface to log user actions across the application.
"""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.audit_log import AuditAction
from app.db.repositories.audit_log_repo import AuditLogRepository

log = get_logger(__name__)


class AuditService:
    """Service for creating audit log entries.

    This service provides a centralized way to log user actions for security
    and compliance. It should be called whenever a user performs a significant
    action (account management, subscription changes, media operations, etc.).

    Usage:
        audit = AuditService(session)
        await audit.log(
            user_id=user.id,
            action=AuditAction.ACCOUNT_CONNECTED,
            telegram_account_id=account.id,
            details={"phone": "+1***"},
            ip_address=request.client.host,
            user_agent=request.headers.get("user-agent"),
        )
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = AuditLogRepository(session)

    async def log(
        self,
        *,
        user_id: int,
        action: AuditAction | str,
        telegram_account_id: int | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Log an audit event.

        This method is fire-and-forget: it logs to the database but does not
        raise exceptions on failure (to avoid disrupting the main operation).

        Args:
            user_id: Application user ID who performed the action.
            action: Type of action (AuditAction enum or string).
            telegram_account_id: Associated Telegram account ID, if applicable.
            details: Additional context/metadata as dict.
            ip_address: Client IP address.
            user_agent: Client user agent string.
        """
        try:
            await self._repo.create(
                user_id=user_id,
                action=action,
                telegram_account_id=telegram_account_id,
                details=details,
                ip_address=ip_address,
                user_agent=user_agent,
            )
            log.debug(
                "audit_log_created",
                user_id=user_id,
                action=action,
                telegram_account_id=telegram_account_id,
            )
        except Exception as exc:
            # Never fail the main operation due to audit logging failure
            log.error(
                "audit_log_failed",
                user_id=user_id,
                action=action,
                error=str(exc),
            )

    # Convenience methods for common actions

    async def log_account_connected(
        self,
        user_id: int,
        telegram_account_id: int,
        *,
        phone_masked: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Log when a user connects a Telegram account."""
        await self.log(
            user_id=user_id,
            action=AuditAction.ACCOUNT_CONNECTED,
            telegram_account_id=telegram_account_id,
            details={"phone_masked": phone_masked} if phone_masked else None,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def log_account_disconnected(
        self,
        user_id: int,
        telegram_account_id: int,
        *,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Log when a user disconnects a Telegram account."""
        await self.log(
            user_id=user_id,
            action=AuditAction.ACCOUNT_DISCONNECTED,
            telegram_account_id=telegram_account_id,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def log_subscription_granted(
        self,
        user_id: int,
        *,
        plan: str | None = None,
        expires_at: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Log when a subscription is granted to a user."""
        await self.log(
            user_id=user_id,
            action=AuditAction.SUBSCRIPTION_GRANTED,
            details={"plan": plan, "expires_at": expires_at} if plan or expires_at else None,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def log_subscription_revoked(
        self,
        user_id: int,
        *,
        reason: str | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> None:
        """Log when a subscription is revoked."""
        await self.log(
            user_id=user_id,
            action=AuditAction.SUBSCRIPTION_REVOKED,
            details={"reason": reason} if reason else None,
            ip_address=ip_address,
            user_agent=user_agent,
        )

    async def log_media_saved(
        self,
        user_id: int,
        telegram_account_id: int,
        *,
        media_type: str | None = None,
        source_chat_id: int | None = None,
        source_message_id: int | None = None,
        saved_message_id: int | None = None,
    ) -> None:
        """Log when media is successfully saved."""
        await self.log(
            user_id=user_id,
            action=AuditAction.MEDIA_SAVED,
            telegram_account_id=telegram_account_id,
            details={
                "media_type": media_type,
                "source_chat_id": source_chat_id,
                "source_message_id": source_message_id,
                "saved_message_id": saved_message_id,
            }
            if media_type or source_chat_id or source_message_id or saved_message_id
            else None,
        )

    async def log_media_save_failed(
        self,
        user_id: int,
        telegram_account_id: int,
        *,
        media_type: str | None = None,
        error: str | None = None,
    ) -> None:
        """Log when media save fails."""
        await self.log(
            user_id=user_id,
            action=AuditAction.MEDIA_SAVE_FAILED,
            telegram_account_id=telegram_account_id,
            details={"media_type": media_type, "error": error} if media_type or error else None,
        )
