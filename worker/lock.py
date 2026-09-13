"""
worker.lock
~~~~~~~~~~~
Distributed account lock using Redis SET NX PX.

Design:
  - Key format: "worker:account:lock:{account_id}"
  - TTL: 30 seconds (LOCK_TTL_MS)
  - Heartbeat: every 10 seconds (HEARTBEAT_INTERVAL_S) refreshes the TTL
  - Acquire uses SET NX with a unique owner token so only the holder can release
  - Release uses a Lua script for atomicity (check owner, then DEL)

Fallback:
  - If Redis is None (not configured), always returns acquired=True with a warning.
    This allows single-worker deployments without Redis.

CLAUDE.md §9: "One TelegramClient per Telegram account." This lock enforces
that invariant across multiple worker processes.
"""

from __future__ import annotations

import asyncio
import uuid

import redis.asyncio as aioredis

from app.core.logging import get_logger

log = get_logger(__name__)

# Lock TTL — must be much longer than HEARTBEAT_INTERVAL_S
LOCK_TTL_MS = 30_000       # 30 seconds
HEARTBEAT_INTERVAL_S = 10  # refresh every 10 seconds

# Lua script: delete key only if its value matches the owner token (atomic check-and-delete)
_RELEASE_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("del", KEYS[1])
else
    return 0
end
"""

# Lua script: expire key only if its value matches the owner token (atomic check-and-pexpire)
_REFRESH_SCRIPT = """
if redis.call("get", KEYS[1]) == ARGV[1] then
    return redis.call("pexpire", KEYS[1], ARGV[2])
else
    return 0
end
"""


def _lock_key(account_id: int) -> str:
    return f"worker:account:lock:{account_id}"


class AccountLock:
    """Distributed Redis lock — one lock per Telegram account.

    Usage::

        lock = AccountLock(redis_client)
        token = await lock.acquire(account_id)
        if token is None:
            # Another worker already owns this account
            return

        # Start heartbeat to keep lock alive
        heartbeat_task = asyncio.create_task(lock.heartbeat(account_id, token))
        try:
            # ... run Telethon client ...
        finally:
            heartbeat_task.cancel()
            await lock.release(account_id, token)
    """

    def __init__(self, redis: aioredis.Redis | None) -> None:
        self._redis = redis
        self._fallback_mode = redis is None
        if self._fallback_mode:
            log.warning(
                "account_lock_fallback",
                msg="Redis not available — AccountLock running in single-worker fallback mode. "
                    "DO NOT run multiple worker instances without Redis.",
            )

    async def acquire(self, account_id: int) -> str | None:
        """Try to acquire the lock for the given account.

        Returns:
            A unique owner token (str) if acquired, or None if already held.

        Fallback:
            If Redis is not configured, always returns a dummy token and logs a warning.
        """
        if self._fallback_mode:
            token = str(uuid.uuid4())
            log.debug("account_lock_fallback_acquired", account_id=account_id)
            return token

        key = _lock_key(account_id)
        token = str(uuid.uuid4())

        result = await self._redis.set(key, token, px=LOCK_TTL_MS, nx=True)
        if result:
            log.info("account_lock_acquired", account_id=account_id)
            return token

        log.debug("account_lock_already_held", account_id=account_id)
        return None

    async def release(self, account_id: int, token: str) -> bool:
        """Release the lock. Only succeeds if the token matches (owner check).

        Returns:
            True if this caller owned and released the lock.
            False if the lock was already expired/released or owned by another worker.
        """
        if self._fallback_mode:
            log.debug("account_lock_fallback_released", account_id=account_id)
            return True

        key = _lock_key(account_id)
        result = await self._redis.eval(_RELEASE_SCRIPT, 1, key, token)
        released = bool(result)
        log.info("account_lock_released", account_id=account_id, success=released)
        return released

    async def refresh(self, account_id: int, token: str) -> bool:
        """Extend the lock TTL if this caller still owns it (heartbeat).

        Returns:
            True if TTL was refreshed (still the owner).
            False if the lock expired or was taken by another worker.
        """
        if self._fallback_mode:
            return True

        key = _lock_key(account_id)
        result = await self._redis.eval(_REFRESH_SCRIPT, 1, key, token, LOCK_TTL_MS)
        if not bool(result):
            log.warning(
                "account_lock_heartbeat_lost",
                account_id=account_id,
                msg="Lock no longer owned — another worker may have taken over.",
            )
            return False

        log.debug("account_lock_heartbeat_ok", account_id=account_id)
        return True

    async def heartbeat(self, account_id: int, token: str) -> None:
        """Long-running coroutine that refreshes the lock on HEARTBEAT_INTERVAL_S.

        Cancels silently when the task is cancelled (normal shutdown path).
        """
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_S)
                still_owner = await self.refresh(account_id, token)
                if not still_owner:
                    log.error(
                        "account_lock_lost",
                        account_id=account_id,
                        msg="Heartbeat lost lock ownership — worker should stop this account.",
                    )
                    # Signal loss of lock; the supervisor should check this flag
                    break
        except asyncio.CancelledError:
            pass  # Normal shutdown
