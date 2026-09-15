"""
tests.unit.test_phase17
~~~~~~~~~~~~~~~~~~~~~~~~
Phase 17: Production Hardening - Unit tests for new Phase 17 features.

Tests:
- Database connection recovery
- Redis connection recovery
- Audit log service
- Health check integration
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.redis import check_redis_health, reconnect_redis
from app.db.session import check_db_health, reconnect_database
from app.db.models.audit_log import AuditAction, AuditLog
from app.db.repositories.audit_log_repo import AuditLogRepository
from app.services.audit import AuditService


class TestDatabaseConnectionRecovery:
    """Tests for database connection recovery (Phase 17)."""

    @pytest.mark.asyncio
    async def test_check_db_health_function_exists(self):
        """Test that check_db_health function exists and is callable."""
        from app.db.session import check_db_health
        assert callable(check_db_health)

    @pytest.mark.asyncio
    async def test_reconnect_database_function_exists(self):
        """Test that reconnect_database function exists and is callable."""
        from app.db.session import reconnect_database
        assert callable(reconnect_database)


class TestRedisConnectionRecovery:
    """Tests for Redis connection recovery (Phase 17)."""

    @pytest.mark.asyncio
    async def test_check_redis_health_success(self):
        """Test that healthy Redis returns True."""
        with patch("app.core.redis.get_redis_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.ping.return_value = True
            mock_get_client.return_value = mock_client

            result = await check_redis_health()
            assert result is True

    @pytest.mark.asyncio
    async def test_check_redis_health_not_configured(self):
        """Test that unconfigured Redis returns False."""
        with patch("app.core.redis.get_redis_client") as mock_get_client:
            mock_get_client.return_value = None

            result = await check_redis_health()
            assert result is False

    @pytest.mark.asyncio
    async def test_check_redis_health_failure(self):
        """Test that unhealthy Redis returns False."""
        with patch("app.core.redis.get_redis_client") as mock_get_client:
            mock_client = AsyncMock()
            mock_client.ping.side_effect = Exception("Connection failed")
            mock_get_client.return_value = mock_client

            result = await check_redis_health()
            assert result is False

    @pytest.mark.asyncio
    async def test_reconnect_redis_success(self):
        """Test successful Redis reconnection."""
        with patch("app.core.redis.get_redis_client") as mock_get_client, \
             patch("app.core.redis._redis_client", None):
            mock_client = AsyncMock()
            mock_client.ping.return_value = True
            mock_client.aclose = AsyncMock()
            mock_get_client.return_value = mock_client

            result = await reconnect_redis()
            assert result is True


class TestAuditLogService:
    """Tests for audit log service (Phase 17)."""

    @pytest.mark.asyncio
    async def test_audit_log_create(self):
        """Test creating an audit log entry."""
        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.refresh = AsyncMock()

        repo = AuditLogRepository(mock_session)
        audit_log = await repo.create(
            user_id=1,
            action=AuditAction.ACCOUNT_CONNECTED,
            telegram_account_id=100,
            details={"phone_masked": "+1***"},
            ip_address="127.0.0.1",
            user_agent="test-agent",
        )

        assert audit_log.user_id == 1
        assert audit_log.action == "account_connected"
        assert audit_log.telegram_account_id == 100
        mock_session.add.assert_called_once()
        mock_session.flush.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_service_log_account_connected(self):
        """Test convenience method for logging account connection."""
        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.refresh = AsyncMock()

        service = AuditService(mock_session)
        await service.log_account_connected(
            user_id=1,
            telegram_account_id=100,
            phone_masked="+1***",
            ip_address="127.0.0.1",
        )

        mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_service_log_media_saved(self):
        """Test convenience method for logging media save."""
        mock_session = AsyncMock()
        mock_session.flush = AsyncMock()
        mock_session.refresh = AsyncMock()

        service = AuditService(mock_session)
        await service.log_media_saved(
            user_id=1,
            telegram_account_id=100,
            media_type="photo",
            source_chat_id=200,
            source_message_id=300,
            saved_message_id=400,
        )

        mock_session.add.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_service_graceful_failure(self):
        """Test that audit service doesn't raise on database failure."""
        mock_session = AsyncMock()
        mock_session.flush.side_effect = Exception("Database error")

        service = AuditService(mock_session)
        # Should not raise exception
        await service.log(
            user_id=1,
            action=AuditAction.ACCOUNT_CONNECTED,
        )


class TestHealthCheckIntegration:
    """Tests for health check functions (Phase 17)."""

    @pytest.mark.asyncio
    async def test_health_check_functions_exist(self):
        """Test that health check functions are importable and callable."""
        from app.db.session import check_db_health
        from app.core.redis import check_redis_health
        
        assert callable(check_db_health)
        assert callable(check_redis_health)
