"""
app.core.redis
~~~~~~~~~~~~~~
Async Redis client factory.

Provides a process-singleton Redis connection. Returns None gracefully
when REDIS_URL is not configured (allows single-worker mode without Redis).
"""

from __future__ import annotations

import redis.asyncio as aioredis

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_redis_client: aioredis.Redis | None = None


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
        )
        log.info("redis_client_created", url=url.split("@")[-1])  # hide credentials
    return _redis_client


def override_redis_client(client: aioredis.Redis | None) -> None:
    """Replace the singleton with a test client (e.g. fakeredis). Call before any usage."""
    global _redis_client
    _redis_client = client


def reset_redis_client() -> None:
    """Reset the singleton (for tests or graceful reconnect)."""
    global _redis_client
    _redis_client = None
