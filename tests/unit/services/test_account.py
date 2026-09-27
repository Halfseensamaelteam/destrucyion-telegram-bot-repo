"""
tests/unit/services/test_account.py
Unit tests for AccountService — QR login flow (CLAUDE.md §12).
All Telethon calls are mocked. No real Telegram connection is made.
"""

import asyncio

import pytest
from cryptography.fernet import Fernet
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import SessionPasswordNeededError

from app.db.models.telegram_account import TelegramAccountStatus
from app.db.repositories import TelegramAccountRepository, UserRepository
from app.services import account as account_module
from app.services.account import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    AccountService,
    AuthError,
    AuthTimeoutError,
    QrLoginCallbacks,
    SessionError,
)

pytestmark = pytest.mark.asyncio

FAKE_API_ID = 12345678
FAKE_API_HASH = "abcd1234efgh5678ijkl9012mnop3456"


@pytest.fixture(autouse=True)
def _session_encryption_key(monkeypatch):
    """create_account/perform_qr_login encrypt api_hash/session via
    SessionCipher, which requires SESSION_ENCRYPTION_KEY to be set."""
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", valid_fernet_key())
    from app.core.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def valid_fernet_key() -> str:
    return Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# create_account — CLAUDE.md §12.2 (own api_id/hash) + §12.3 (one per user)
# ---------------------------------------------------------------------------

async def test_create_account_stores_own_api_credentials(db_session: AsyncSession, user):
    service = AccountService(db_session)
    acc = await service.create_or_reset_account(
        user_id=user.id, api_id=FAKE_API_ID, api_hash=FAKE_API_HASH
    )

    assert acc.id is not None
    assert acc.api_id == FAKE_API_ID
    # api_hash must be encrypted, never stored as plaintext
    assert acc.api_hash_ciphertext is not None
    assert acc.api_hash_ciphertext != FAKE_API_HASH
    assert acc.status == TelegramAccountStatus.DISCONNECTED


async def test_create_account_rejects_second_account_for_same_user(
    db_session: AsyncSession, user, account
):
    """account fixture already creates an ACTIVE account for `user`."""
    service = AccountService(db_session)
    with pytest.raises(AccountAlreadyExistsError):
        await service.create_or_reset_account(
            user_id=user.id, api_id=FAKE_API_ID, api_hash=FAKE_API_HASH
        )


async def test_create_account_allowed_after_disconnect(db_session: AsyncSession, user, account):
    """Once the existing account is DISCONNECTED, it is RESET and reused —
    the DB's UNIQUE constraint on user_id means a second row can never be
    inserted for the same user; the same row id is reused instead."""
    repo = TelegramAccountRepository(db_session)
    await repo.update_status(account, TelegramAccountStatus.DISCONNECTED)

    service = AccountService(db_session)
    reset_acc = await service.create_or_reset_account(
        user_id=user.id, api_id=FAKE_API_ID, api_hash=FAKE_API_HASH
    )
    assert reset_acc.id == account.id
    assert reset_acc.api_id == FAKE_API_ID
    # Any stale session from the previous connection must be cleared
    assert reset_acc.session_ciphertext is None


# ---------------------------------------------------------------------------
# perform_qr_login — happy path
# ---------------------------------------------------------------------------

def _make_qr_login_mock(me_result, *, expire_once: bool = False, needs_2fa: bool = False):
    """Build a mock Telethon qr_login() return value.

    wait() behavior can be customized:
      - expire_once: first call raises asyncio.TimeoutError, second succeeds
      - needs_2fa: first call raises SessionPasswordNeededError
    """
    qr_login = MagicMock()
    qr_login.url = "tg://login?token=FAKEQRTOKEN"
    qr_login.recreate = AsyncMock()

    calls = {"n": 0}

    async def wait(timeout=None):
        calls["n"] += 1
        if expire_once and calls["n"] == 1:
            raise asyncio.TimeoutError()
        if needs_2fa and calls["n"] == 1:
            raise SessionPasswordNeededError(request=None)
        return me_result

    qr_login.wait = wait
    return qr_login


@pytest.fixture
async def qr_account(db_session: AsyncSession, user):
    """An account with its own api_id/api_hash already on file."""
    from app.core.crypto import SessionCipher

    repo = TelegramAccountRepository(db_session)
    account = await repo.create(user_id=user.id, status=TelegramAccountStatus.DISCONNECTED)
    cipher = SessionCipher.from_settings()
    account = await repo.update(
        account, api_id=FAKE_API_ID, api_hash_ciphertext=cipher.encrypt(FAKE_API_HASH)
    )
    return account


async def test_perform_qr_login_success(db_session: AsyncSession, user, qr_account):
    me = MagicMock(id=999, username="testuser")
    qr_login_obj = _make_qr_login_mock(me)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.qr_login = AsyncMock(return_value=qr_login_obj)
    mock_client.disconnect = AsyncMock()
    mock_client.session.save = MagicMock(return_value="fake_authenticated_session")

    on_qr_ready = AsyncMock()
    get_2fa_password = AsyncMock()

    service = AccountService(db_session)
    with patch.object(service, "_get_client", return_value=mock_client):
        result = await service.perform_qr_login(
            account_id=qr_account.id,
            user_id=user.id,
            callbacks=QrLoginCallbacks(on_qr_ready=on_qr_ready, get_2fa_password=get_2fa_password),
        )

    assert result.status == TelegramAccountStatus.ACTIVE
    assert result.telegram_user_id == 999
    assert result.username == "testuser"
    assert result.session_ciphertext is not None
    on_qr_ready.assert_awaited_once_with(qr_login_obj.url)
    get_2fa_password.assert_not_awaited()
    mock_client.disconnect.assert_awaited_once()


async def test_perform_qr_login_uses_account_own_credentials(
    db_session: AsyncSession, user, qr_account
):
    """_get_client must be called with THIS account's api_id/api_hash, not globals."""
    me = MagicMock(id=999, username="testuser")
    qr_login_obj = _make_qr_login_mock(me)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.qr_login = AsyncMock(return_value=qr_login_obj)
    mock_client.session.save = MagicMock(return_value="fake_session")

    service = AccountService(db_session)
    with patch.object(service, "_get_client", return_value=mock_client) as mock_get_client:
        await service.perform_qr_login(
            account_id=qr_account.id,
            user_id=user.id,
            callbacks=QrLoginCallbacks(
                on_qr_ready=AsyncMock(), get_2fa_password=AsyncMock()
            ),
        )

    _, kwargs = mock_get_client.call_args
    assert kwargs["api_id"] == FAKE_API_ID
    assert kwargs["api_hash"] == FAKE_API_HASH


async def test_perform_qr_login_refreshes_expired_token(
    db_session: AsyncSession, user, qr_account
):
    me = MagicMock(id=999, username="testuser")
    qr_login_obj = _make_qr_login_mock(me, expire_once=True)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.qr_login = AsyncMock(return_value=qr_login_obj)
    mock_client.session.save = MagicMock(return_value="fake_session")

    on_qr_ready = AsyncMock()

    service = AccountService(db_session)
    with patch.object(service, "_get_client", return_value=mock_client):
        result = await service.perform_qr_login(
            account_id=qr_account.id,
            user_id=user.id,
            callbacks=QrLoginCallbacks(on_qr_ready=on_qr_ready, get_2fa_password=AsyncMock()),
        )

    assert result.status == TelegramAccountStatus.ACTIVE
    qr_login_obj.recreate.assert_awaited_once()
    # Shown once initially, once again after refresh
    assert on_qr_ready.await_count == 2


async def test_perform_qr_login_handles_2fa(db_session: AsyncSession, user, qr_account):
    me = MagicMock(id=999, username="testuser")
    qr_login_obj = _make_qr_login_mock(me, needs_2fa=True)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.qr_login = AsyncMock(return_value=qr_login_obj)
    mock_client.sign_in = AsyncMock()
    mock_client.get_me = AsyncMock(return_value=me)
    mock_client.session.save = MagicMock(return_value="fake_session")

    get_2fa_password = AsyncMock(return_value="my-2fa-password")

    service = AccountService(db_session)
    with patch.object(service, "_get_client", return_value=mock_client):
        result = await service.perform_qr_login(
            account_id=qr_account.id,
            user_id=user.id,
            callbacks=QrLoginCallbacks(on_qr_ready=AsyncMock(), get_2fa_password=get_2fa_password),
        )

    assert result.status == TelegramAccountStatus.ACTIVE
    get_2fa_password.assert_awaited_once()
    # sign_in must be called on the SAME still-connected client, not a new one
    mock_client.sign_in.assert_awaited_once_with(password="my-2fa-password")


async def test_perform_qr_login_times_out_if_never_scanned(
    db_session: AsyncSession, user, qr_account, monkeypatch
):
    # Shrink the timeouts so the test doesn't actually take 5 minutes, but
    # keep them > 0 so the while loop actually runs at least once.
    monkeypatch.setattr(account_module, "QR_LOGIN_TOKEN_WAIT_SECONDS", 0.01)
    monkeypatch.setattr(account_module, "QR_LOGIN_TOTAL_TIMEOUT_SECONDS", 0.01)

    qr_login_obj = MagicMock()
    qr_login_obj.url = "tg://login?token=NEVERSCANNED"
    qr_login_obj.recreate = AsyncMock()

    async def always_times_out(timeout=None):
        raise asyncio.TimeoutError()

    qr_login_obj.wait = always_times_out

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.qr_login = AsyncMock(return_value=qr_login_obj)

    service = AccountService(db_session)
    with patch.object(service, "_get_client", return_value=mock_client):
        with pytest.raises(AuthTimeoutError):
            await service.perform_qr_login(
                account_id=qr_account.id,
                user_id=user.id,
                callbacks=QrLoginCallbacks(on_qr_ready=AsyncMock(), get_2fa_password=AsyncMock()),
            )


async def test_perform_qr_login_tenant_isolation(db_session: AsyncSession, qr_account):
    """A different user_id must not be able to drive someone else's account."""
    service = AccountService(db_session)
    with pytest.raises(AccountNotFoundError):
        await service.perform_qr_login(
            account_id=qr_account.id,
            user_id=qr_account.user_id + 99999,
            callbacks=QrLoginCallbacks(on_qr_ready=AsyncMock(), get_2fa_password=AsyncMock()),
        )


# ---------------------------------------------------------------------------
# get_account_api_credentials
# ---------------------------------------------------------------------------

async def test_get_account_api_credentials_decrypts(db_session: AsyncSession, qr_account):
    service = AccountService(db_session)
    api_id, api_hash = service.get_account_api_credentials(qr_account)
    assert api_id == FAKE_API_ID
    assert api_hash == FAKE_API_HASH


async def test_get_account_api_credentials_missing_raises(db_session: AsyncSession, account):
    """`account` fixture has no api_id/api_hash set."""
    service = AccountService(db_session)
    with pytest.raises(SessionError):
        service.get_account_api_credentials(account)


# ---------------------------------------------------------------------------
# disconnect_account — unchanged behavior, still tenant-isolated
# ---------------------------------------------------------------------------

async def test_disconnect_account(db_session: AsyncSession, user, account):
    service = AccountService(db_session)
    result = await service.disconnect_account(account_id=account.id, user_id=user.id)
    assert result.status == TelegramAccountStatus.DISCONNECTED


async def test_disconnect_account_tenant_isolation(db_session: AsyncSession, account):
    service = AccountService(db_session)
    with pytest.raises(AccountNotFoundError):
        await service.disconnect_account(account_id=account.id, user_id=account.user_id + 99999)


# ---------------------------------------------------------------------------
# try_resume_session — reuse a previously stored session instead of forcing
# a fresh QR login (user-requested UX improvement).
# ---------------------------------------------------------------------------

async def test_try_resume_session_success(db_session: AsyncSession, user, qr_account):
    """A still-valid stored session resumes without any QR/network round-trip
    beyond the authorization check, and marks the account ACTIVE again."""
    from app.core.crypto import SessionCipher

    cipher = SessionCipher.from_settings()
    repo = TelegramAccountRepository(db_session)
    await repo.update(qr_account, session_ciphertext=cipher.encrypt("some_session_string"))
    await repo.update_status(qr_account, TelegramAccountStatus.DISCONNECTED)

    me = MagicMock(id=999, username="testuser")
    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=True)
    mock_client.get_me = AsyncMock(return_value=me)
    mock_client.disconnect = AsyncMock()

    service = AccountService(db_session)
    with patch("app.services.account.TelegramClient", return_value=mock_client), \
         patch("app.services.account.StringSession", return_value=MagicMock()):
        resumed = await service.try_resume_session(account_id=qr_account.id, user_id=user.id)

    assert resumed is True
    await db_session.refresh(qr_account)
    assert qr_account.status == TelegramAccountStatus.ACTIVE
    assert qr_account.telegram_user_id == 999
    mock_client.disconnect.assert_awaited_once()


async def test_try_resume_session_revoked_returns_false(db_session: AsyncSession, user, qr_account):
    """A revoked/invalid session must return False (not raise) so the
    caller can fall back to a fresh QR login."""
    from app.core.crypto import SessionCipher

    cipher = SessionCipher.from_settings()
    repo = TelegramAccountRepository(db_session)
    await repo.update(qr_account, session_ciphertext=cipher.encrypt("stale_session_string"))
    await repo.update_status(qr_account, TelegramAccountStatus.DISCONNECTED)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.is_user_authorized = AsyncMock(return_value=False)
    mock_client.disconnect = AsyncMock()

    service = AccountService(db_session)
    with patch("app.services.account.TelegramClient", return_value=mock_client), \
         patch("app.services.account.StringSession", return_value=MagicMock()):
        resumed = await service.try_resume_session(account_id=qr_account.id, user_id=user.id)

    assert resumed is False
    await db_session.refresh(qr_account)
    # Must remain DISCONNECTED — caller decides whether to launch fresh QR.
    assert qr_account.status == TelegramAccountStatus.DISCONNECTED


async def test_try_resume_session_no_session_returns_false(db_session: AsyncSession, user, qr_account):
    """An account with no stored session at all can't be resumed."""
    service = AccountService(db_session)
    resumed = await service.try_resume_session(account_id=qr_account.id, user_id=user.id)
    assert resumed is False
