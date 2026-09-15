"""
app.db.repositories.audit_log_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for the `audit_logs` table.

Phase 17: Production Hardening - Audit log data access layer.
"""

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.audit_log import AuditAction, AuditLog


class AuditLogRepository:
    """Data access layer for AuditLog records."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self,
        *,
        user_id: int,
        action: AuditAction | str,
        telegram_account_id: int | None = None,
        details: dict[str, Any] | None = None,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> AuditLog:
        """Create a new audit log entry.

        Args:
            user_id: Application user ID who performed the action.
            action: Type of action (AuditAction enum or string).
            telegram_account_id: Associated Telegram account ID, if applicable.
            details: Additional context/metadata as dict (will be JSON-serialized).
            ip_address: Client IP address.
            user_agent: Client user agent string.

        Returns:
            The created AuditLog instance.
        """
        import json

        details_json = json.dumps(details) if details else None

        audit_log = AuditLog(
            user_id=user_id,
            action=action.value if isinstance(action, AuditAction) else action,
            telegram_account_id=telegram_account_id,
            details=details_json,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self._session.add(audit_log)
        await self._session.flush()
        await self._session.refresh(audit_log)
        return audit_log

    async def get_by_user(
        self,
        user_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Get audit日志 for a specific user, paginated.

        Args:
            user_id: Application user ID.
            limit: Maximum number of records to return.
            offset: Number of records to skip.

        Returns:
            List of AuditLog entries, ordered by created_at DESC.
        """
        result = await self._session.execute(
            select(AuditLog)
            .where(AuditLog.user_id == user_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_by_action(
        self,
        action: AuditAction | str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Get audit logs for a specific action type, paginated.

        Args:
            action: Type of action to filter by.
            limit: Maximum number of records to return.
            offset: Number of records to skip.

        Returns:
            List of AuditLog entries, ordered by created_at DESC.
        """
        action_value = action.value if isinstance(action, AuditAction) else action
        result = await self._session.execute(
            select(AuditLog)
            .where(AuditLog.action == action_value)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_by_telegram_account(
        self,
        telegram_account_id: int,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Get audit logs for a specific Telegram account, paginated.

        Args:
            telegram_account_id: Telegram account ID to filter by.
            limit: Maximum number of records to return.
            offset: Number of records to skip.

        Returns:
            List of AuditLog entries, ordered by created_at DESC.
        """
        result = await self._session.execute(
            select(AuditLog)
            .where(AuditLog.telegram_account_id == telegram_account_id)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_recent(
        self,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AuditLog]:
        """Get recent audit logs across all users, paginated.

        Args:
            limit: Maximum number of records to return.
            offset: Number of records to skip.

        Returns:
            List of AuditLog entries, ordered by created_at DESC.
        """
        result = await self._session.execute(
            select(AuditLog)
            .order_by(AuditLog.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def get_by_date_range(
        self,
        start: datetime,
        end: datetime,
        *,
        user_id: int | None = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        """Get audit logs within a date range, optionally filtered by user.

        Args:
            start: Start datetime (inclusive).
            end: End datetime (inclusive).
            user_id: Optional user ID filter.
            limit: Maximum number of records to return.

        Returns:
            List of AuditLog entries, ordered by created_at DESC.
        """
        query = select(AuditLog).where(
            AuditLog.created_at >= start,
            AuditLog.created_at <= end,
        )
        if user_id is not None:
            query = query.where(AuditLog.user_id == user_id)

        result = await self._session.execute(
            query.order_by(AuditLog.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
