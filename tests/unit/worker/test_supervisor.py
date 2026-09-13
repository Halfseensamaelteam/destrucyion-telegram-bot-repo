"""
tests/unit/worker/test_supervisor.py
Tests for TelegramClientManager. All Telethon dependencies and DB calls mocked.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from worker.supervisor import (
    TelegramClientManager,
    ClientState,
    MAX_RECONNECT_ATTEMPTS,
)
from app.core.crypto import SessionEncryptionError

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------------
# Fixtures & Mocks
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_cipher():
    cipher = MagicMock()
    cipher.decrypt.return_value = "decrypted_session_string"
    return cipher


@pytest.fixture
def mock_session_factory():
    session = AsyncMock(spec=AsyncSession)
    return MagicMock(return_value=session)


@pytest.fixture
def manager(mock_session_factory, mock_cipher):
    from worker.lock import AccountLock
    dummy_lock = AccountLock(None)
    return TelegramClientManager(
        session_factory=mock_session_factory,
        cipher=mock_cipher,
        handler_factory=None,
        account_lock=dummy_lock,
    )


def make_account(id=1, user_id=1, status=TelegramAccountStatus.ACTIVE, has_session=True):
    acc = MagicMock(spec=TelegramAccount)
    acc.id = id
    acc.user_id = user_id
    acc.status = status
    acc.session_ciphertext = "encrypted_session" if has_session else None
    return acc


# ---------------------------------------------------------------------------
# Tests: start() & account loading
# ---------------------------------------------------------------------------

async def test_start_loads_accounts(manager):
    acc1 = make_account(id=1)
    acc2 = make_account(id=2)

    with patch.object(manager, "_load_active_accounts", new_callable=AsyncMock) as m_load, \
         patch.object(manager, "_start_account", new_callable=AsyncMock) as m_start:
        
        m_load.return_value = [acc1, acc2]
        
        await manager.start()
        
        m_load.assert_called_once()
        assert m_start.call_count == 2
        m_start.assert_any_call(acc1)
        m_start.assert_any_call(acc2)


async def test_start_account_no_subscription_skips(manager):
    acc = make_account()
    with patch.object(manager, "_check_subscription", new_callable=AsyncMock) as m_sub:
        m_sub.return_value = False
        
        await manager._start_account(acc)
        
        assert acc.id not in manager._clients


async def test_start_account_no_session_skips(manager):
    acc = make_account(has_session=False)
    with patch.object(manager, "_check_subscription", new_callable=AsyncMock) as m_sub:
        m_sub.return_value = True
        
        await manager._start_account(acc)
        
        assert acc.id not in manager._clients


async def test_start_account_decryption_fails_marks_error(manager, mock_cipher):
    acc = make_account()
    mock_cipher.decrypt.side_effect = SessionEncryptionError("Invalid key")
    
    with patch.object(manager, "_check_subscription", new_callable=AsyncMock) as m_sub, \
         patch.object(manager, "_mark_error", new_callable=AsyncMock) as m_err:
        m_sub.return_value = True
        
        await manager._start_account(acc)
        
        m_err.assert_called_once_with(acc.id, "Session decryption failed.")
        assert acc.id not in manager._clients


async def test_start_account_success_launches_task(manager, monkeypatch):
    acc = make_account(id=99)
    
    # Mock Telethon client creation
    mock_client = AsyncMock()
    mock_client_class = MagicMock(return_value=mock_client)
    monkeypatch.setattr("telethon.TelegramClient", mock_client_class)
    
    # Avoid ValueError from StringSession
    manager._cipher.decrypt.return_value = ""
    
    # Mock settings
    mock_settings = MagicMock()
    mock_settings.telegram_api_id = "123"
    mock_settings.telegram_api_hash = "abc"
    monkeypatch.setattr("app.core.config.get_settings", MagicMock(return_value=mock_settings))

    with patch.object(manager, "_check_subscription", new_callable=AsyncMock) as m_sub, \
         patch.object(manager, "_run_account", new_callable=AsyncMock) as m_run:
        m_sub.return_value = True
        
        await manager._start_account(acc)
        
        assert acc.id in manager._clients
        managed = manager._clients[acc.id]
        assert managed.state == ClientState.STARTING
        assert managed.client == mock_client
        assert managed.task is not None
        assert not managed.task.done()
        
        # Cleanup
        managed.task.cancel()


# ---------------------------------------------------------------------------
# Tests: lifecycle (_run_account, disconnect, reconnect)
# ---------------------------------------------------------------------------

async def test_run_account_not_authorized(manager):
    acc = make_account(id=1)
    
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = False
    
    manager._clients[1] = MagicMock()
    manager._clients[1].account_id = 1
    manager._clients[1].client = mock_client
    manager._clients[1].reconnect_attempts = 0
    
    with patch.object(manager, "_persist_error", new_callable=AsyncMock) as m_err:
        await manager._run_account(manager._clients[1])
        
        assert manager._clients[1].state == ClientState.ERROR
        assert manager._clients[1].last_error == "Session expired or not authorized."
        m_err.assert_called_once()


async def test_run_account_normal_disconnect_stops(manager):
    """If client.run_until_disconnected() returns normally, it should mark STOPPED."""
    mock_client = AsyncMock()
    mock_client.is_user_authorized.return_value = True
    
    manager._clients[1] = MagicMock()
    manager._clients[1].account_id = 1
    manager._clients[1].client = mock_client
    manager._clients[1].reconnect_attempts = 0
    
    manager._stop_event.set() # Stop the while loop after one iteration

    with patch.object(manager, "_persist_connected", new_callable=AsyncMock):
        await manager._run_account(manager._clients[1])
        
        assert manager._clients[1].state == ClientState.STOPPED


async def test_run_account_reconnect_loop(manager, monkeypatch):
    """Test that exception during connect triggers reconnect backoff, and eventually ERROR."""
    monkeypatch.setattr("worker.supervisor.RECONNECT_BASE_DELAY", 0.01) # fast tests
    
    mock_client = AsyncMock()
    # Raise exception to trigger reconnect logic during connect
    mock_client.connect.side_effect = ConnectionError("Network down")
    
    manager._clients[1] = MagicMock()
    manager._clients[1].account_id = 1
    manager._clients[1].client = mock_client
    manager._clients[1].reconnect_attempts = 0
    
    with patch.object(manager, "_persist_connected", new_callable=AsyncMock), \
         patch.object(manager, "_persist_error", new_callable=AsyncMock) as m_err:
        
        await manager._run_account(manager._clients[1])
        
        assert manager._clients[1].state == ClientState.ERROR
        assert manager._clients[1].reconnect_attempts == MAX_RECONNECT_ATTEMPTS + 1
        m_err.assert_called_once()


# ---------------------------------------------------------------------------
# Tests: stop()
# ---------------------------------------------------------------------------

async def test_stop_cancels_all_tasks(manager):
    task1 = asyncio.create_task(asyncio.sleep(10))
    task2 = asyncio.create_task(asyncio.sleep(10))
    
    manager._clients[1] = MagicMock(account_id=1, task=task1, client=AsyncMock())
    manager._clients[2] = MagicMock(account_id=2, task=task2, client=AsyncMock())
    
    await manager.stop()
    
    assert manager._stop_event.is_set()
    assert task1.cancelled()
    assert task2.cancelled()
    assert len(manager._clients) == 0


# ---------------------------------------------------------------------------
# Tests: API access
# ---------------------------------------------------------------------------

async def test_get_client_returns_only_running(manager):
    manager._clients[1] = MagicMock(state=ClientState.RUNNING, client="RunningClient")
    manager._clients[2] = MagicMock(state=ClientState.STARTING, client="StartingClient")
    manager._clients[3] = MagicMock(state=ClientState.STOPPED, client="StoppedClient")
    
    assert manager.get_client(1) == "RunningClient"
    assert manager.get_client(2) is None
    assert manager.get_client(3) is None
    assert manager.get_client(4) is None


async def test_all_states(manager):
    manager._clients[1] = MagicMock(state=ClientState.RUNNING)
    manager._clients[2] = MagicMock(state=ClientState.ERROR)
    
    states = manager.all_states()
    assert states == {1: "RUNNING", 2: "ERROR"}
