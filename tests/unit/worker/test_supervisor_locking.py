"""
tests/unit/worker/test_supervisor_locking.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Tests verifying TelegramClientManager uses AccountLock properly.
"""

import asyncio
from datetime import datetime, timezone, timedelta

import pytest
from fakeredis import FakeAsyncRedis

from worker.supervisor import TelegramClientManager, ClientState
from worker.lock import AccountLock
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.core.crypto import SessionCipher


from unittest.mock import AsyncMock

@pytest.fixture
def shared_redis():
    # Mocking Redis shared state across two locks
    client = AsyncMock()
    
    # State tracking
    storage = {}
    
    async def mock_set(key, value, px=None, nx=False):
        if nx and key in storage:
            return None
        storage[key] = value
        return True
        
    async def mock_eval(script, numkeys, key, token):
        if storage.get(key) == token:
            del storage[key]
            return 1
        return 0
        
    client.set.side_effect = mock_set
    client.eval.side_effect = mock_eval
    
    return client


@pytest.fixture
def mock_cipher():
    # Use a dummy cipher for testing
    import base64
    key = base64.urlsafe_b64encode(b"0" * 32).decode()
    return SessionCipher(key)


@pytest.mark.asyncio
async def test_supervisor_skips_account_if_lock_taken(db_session, shared_redis, mock_cipher, monkeypatch):
    """
    Simulate two worker instances sharing the same Redis instance.
    Worker 1 acquires the lock and starts the account.
    Worker 2 should fail to acquire the lock and skip the account.
    """
    from unittest.mock import MagicMock
    # Mock settings to prevent Telethon ValueError
    mock_settings = MagicMock()
    mock_settings.telegram_api_id = "123"
    mock_settings.telegram_api_hash = "abc"
    monkeypatch.setattr("app.core.config.get_settings", MagicMock(return_value=mock_settings))

    # 1. Setup DB state (active subscription + account)
    from app.db.models.user import User
    user = User(telegram_user_id=1, username="test", first_name="test")
    db_session.add(user)
    await db_session.flush()

    sub = Subscription(
        user_id=user.id,
        plan=SubscriptionPlan.LIFETIME,
        status=SubscriptionStatus.ACTIVE,
        starts_at=datetime.now(timezone.utc) - timedelta(days=1),
        expires_at=datetime.now(timezone.utc) + timedelta(days=365),
    )
    db_session.add(sub)

    # Use an empty session string format that StringSession accepts
    enc_session = mock_cipher.encrypt("")
    account = TelegramAccount(
        user_id=user.id,
        phone_masked="+123",
        status=TelegramAccountStatus.ACTIVE,
        session_ciphertext=enc_session,
    )
    db_session.add(account)
    await db_session.commit()
    await db_session.refresh(account)

    # 2. Worker 1 starts
    lock1 = AccountLock(shared_redis)
    def factory1():
        # Async generator mock wrapper
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def _f():
            yield db_session
        return _f()
        
    manager1 = TelegramClientManager(lambda: factory1(), cipher=mock_cipher, account_lock=lock1)
    
    # We call _start_account manually to avoid full manager start() looping
    await manager1._start_account(account)

    # Worker 1 should have started it
    state1 = manager1.get_state(account.id)
    assert state1 is not None

    # 3. Worker 2 starts
    lock2 = AccountLock(shared_redis)
    manager2 = TelegramClientManager(lambda: factory1(), cipher=mock_cipher, account_lock=lock2)
    
    await manager2._start_account(account)

    # Worker 2 should NOT have started it because Worker 1 holds the lock
    state2 = manager2.get_state(account.id)
    assert state2 is None

    # 4. Stop Worker 1 to cleanup tasks
    await manager1.stop()
