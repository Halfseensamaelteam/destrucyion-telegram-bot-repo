"""
app.core.redis
~~~~~~~~~~~~~~
Async Redis client factory.

Provides a process-singleton Redis connection. Returns None gracefully
when REDIS_URL is not configured (allows single-worker mode without Redis).

Phase 17: Production Hardening - Added connection recovery and health check.
"""

from __future__ import annotations

import asyncio

import redis.asyncio as aioredis
from redis.exceptions import ConnectionError as RedisConnectionError

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_redis_client: aioredis.Redis | None = None
# Reconnect lock to prevent multiple simultaneous reconnect attempts
_redis_reconnect_lock = asyncio.Lock()
# Maximum reconnect attempts before giving up
_MAX_RECONNECT_ATTEMPTS = 3
# Delay between reconnect attempts (seconds)
_RECONNECT_DELAY = 2.0


def get_redis_client() -> aioredis.Redis | None:
    """Return the singleton async Redis client.

    Returns None if Redis is not configured or unavailable.
    Callers must treat None as "Redis not available" and fall back gracefully.
    """
    global _redis_client
    if _redis_client is None:
        settings = get_settings()
        url = settings.redis_url
        if not url:
            log.warning("redis_not_configured", msg="REDIS_URL is empty — running without Redis")
            return None
        _redis_client = aioredis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            socket_keepalive=True,  # Phase 17: Enable TCP keepalive
            health_check_interval=30,  # Phase 17: Check connection health every 30s
        )
        log.info("redis_client_created", url=url.split("@")[-1])  # hide credentials
    return _redis_client


def override_redis_client(client: aioredis.Redis | None) -> None:
    """Replace the singleton with a test client (e.g. fakeredis). Call before any usage."""
    global _redis_client
    _redis_client = client


def reset_redis_client() -> None:
    """Reset the singleton (for tests or graceful reconnect).
    
    Phase 17: Also closes the existing connection if present.
    """
    global _redis_client
    if _redis_client is not None:
        # Close the connection gracefully
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(_redis_client.aclose())
        except Exception:
            pass  # Best effort close
    _redis_client = None


# ----------------------------------------------------------------------
# Phase 17: Redis Connection Recovery
# ----------------------------------------------------------------------


async def check_redis_health() -> bool:
    """Check if the Redis connection is healthy.

    Returns:
        True if Redis is reachable, False otherwise.
    """
    client = get_redis_client()
    if client is None:
        return False  # Redis not configured
    
    try:
        await client.ping()
        return True
    except (RedisConnectionError, Exception) as exc:
        log.warning("redis_health_check_failed", error=str(exc))
        return False


async def reconnect_redis() -> bool:
    """Attempt to reconnect to Redis.

    This function closes the current connection and creates a new one.
    It uses a lock to prevent multiple simultaneous reconnect attempts.

    Returns:
        True if reconnection succeeded, False otherwise.
    """
    global _redis_client

    async with _redis_reconnect_lock:
        # Check if already reconnected by another caller
        if await check_redis_health():
            log.info("redis_reconnect_already_healthy")
            return True

        log.warning("redis_reconnect_attempting")
        
        for attempt in range(_MAX_RECONNECT_ATTEMPTS):
            try:
                # Close old connection
                if _redis_client is not None:
                    await _redis_client.aclose()
                
                # Reset global to force recreation
                _redis_client = None
                
                # Create new connection
                new_client = get_redis_client()
                if new_client is None:
                    log.warning("redis_reconnect_not_configured")
                    return False
                
                # Test the new connection
                await new_client.ping()
                
                log.info("redis_reconnect_success", attempt=attempt + 1)
                return True
                
            except Exception as exc:
                log.warning(
                    "redis_reconnect_failed",
                    attempt=attempt + 1,
                    max_attempts=_MAX_RECONNECT_ATTEMPTS,
                    error=str(exc),
                )
                if attempt < _MAX_RECONNECT_ATTEMPTS - 1:
                    await asyncio.sleep(_RECONNECT_DELAY)
        
        log.error("redis_reconnect_gave_up", max_attempts=_MAX_RECONNECT_ATTEMPTS)
        return False
