"""
tests/unit/worker/test_lock.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests for the distributed AccountLock.
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock
from worker.lock import AccountLock, LOCK_TTL_MS

@pytest.fixture
def redis_client():
    client = AsyncMock()
    return client


@pytest.mark.asyncio
async def test_acquire_and_release(redis_client):
    lock = AccountLock(redis_client)
    account_id = 100

    # First acquire succeeds
    redis_client.set.return_value = True
    token = await lock.acquire(account_id)
    assert token is not None
    redis_client.set.assert_called_with(f"worker:account:lock:{account_id}", token, px=LOCK_TTL_MS, nx=True)

    # Second acquire fails
    redis_client.set.return_value = None
    token2 = await lock.acquire(account_id)
    assert token2 is None

    # Release with wrong token fails
    redis_client.eval.return_value = 0
    released_wrong = await lock.release(account_id, "wrong-token")
    assert not released_wrong

    # Release with correct token succeeds
    redis_client.eval.return_value = 1
    released_correct = await lock.release(account_id, token)
    assert released_correct


@pytest.mark.asyncio
async def test_heartbeat_refreshes_ttl(redis_client):
    lock = AccountLock(redis_client)
    account_id = 200

    redis_client.set.return_value = True
    token = await lock.acquire(account_id)
    assert token is not None

    # Refresh should succeed because we own it
    redis_client.get.return_value = token
    redis_client.pexpire.return_value = True
    refreshed = await lock.refresh(account_id, token)
    assert refreshed is True

    # After release, refresh should fail
    redis_client.get.return_value = "other-token"
    refreshed_after = await lock.refresh(account_id, token)
    assert refreshed_after is False


@pytest.mark.asyncio
async def test_lock_fallback_mode_when_redis_is_none():
    """Verify lock works gracefully without Redis (single-worker mode)."""
    lock = AccountLock(None)
    account_id = 300

    # Acquire should return a dummy token
    token1 = await lock.acquire(account_id)
    assert token1 is not None

    # Second acquire should ALSO return a dummy token (because there is no shared state)
    # This is expected in fallback mode, which shouldn't be used with multiple workers
    token2 = await lock.acquire(account_id)
    assert token2 is not None

    # Release always succeeds
    assert await lock.release(account_id, token1)

    # Refresh always succeeds
    assert await lock.refresh(account_id, token2)
