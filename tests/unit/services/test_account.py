"""
tests/unit/services/test_account.py
Unit tests for AccountService — all Telethon calls are mocked.
No real Telegram connection is made.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cryptography.fernet import Fernet

from sqlalchemy.ext.asyncio import AsyncSession
from telethon.errors import (
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from app.db.models.telegram_account import TelegramAccountStatus
from app.db.repositories import TelegramAccountRepository, UserRepository
from app.services.account import (
    AccountNotFoundError,
    AccountService,
    AuthError,
    SessionError,
    _mask_phone,
)

pytestmark = pytest.mark.asyncio

FAKE_SESSION_STR = ""  # Valid empty Telethon StringSession (unauthenticated).
FAKE_PHONE = "+628123456789"
FAKE_CODE = "12345"
FAKE_HASH = "abc123hash"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------



def valid_fernet_key() -> str:
    return Fernet.generate_key().decode()


# ---------------------------------------------------------------------------
# Phone masking
# ---------------------------------------------------------------------------

def test_mask_phone_standard():
    # +628123456789 → keep first 5 chars, mask 4, show last 4
    assert _mask_phone("+628123456789") == "+6281****6789"


def test_mask_phone_short():
    result = _mask_phone("+1234")
    assert "****" in result or result == "****"


# ---------------------------------------------------------------------------
# create_account
# ---------------------------------------------------------------------------

async def test_create_account_masks_phone(db_session: AsyncSession, user):
    service = AccountService(db_session)
    acc = await service.create_account(user_id=user.id, phone_number=FAKE_PHONE)

    assert acc.id is not None
    assert acc.phone_masked is not None
    assert FAKE_PHONE not in acc.phone_masked  # Never store raw phone
    assert acc.status == TelegramAccountStatus.DISCONNECTED


# ---------------------------------------------------------------------------
# start_auth — Telethon mocked
# ---------------------------------------------------------------------------

async def test_start_auth_returns_phone_code_hash(
    db_session: AsyncSession, user, account, monkeypatch
):
    service = AccountService(db_session)
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "testhash")

    mock_result = MagicMock()
    mock_result.phone_code_hash = FAKE_HASH

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.send_code_request = AsyncMock(return_value=mock_result)
    mock_client.disconnect = AsyncMock()

    with patch.object(service, "_get_client", return_value=mock_client):
        result = await service.start_auth(account.id, user.id, FAKE_PHONE)

    assert result.phone_code_hash == FAKE_HASH
    assert result.account_id == account.id


async def test_start_auth_wrong_user_raises(
    db_session: AsyncSession, user, account
):
    service = AccountService(db_session)
    with pytest.raises(AccountNotFoundError):
        await service.start_auth(account.id, user_id=9999, phone_number=FAKE_PHONE)


# ---------------------------------------------------------------------------
# verify_code — Telethon mocked
# ---------------------------------------------------------------------------

async def test_verify_code_success(
    db_session: AsyncSession, user, account, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "testhash")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", valid_fernet_key())
    monkeypatch.setenv("BOT_TOKEN", "123456:test")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    from app.core.config import get_settings
    get_settings.cache_clear()

    service = AccountService(db_session)

    # Build a mock StringSession that save() returns our fake string
    mock_session = MagicMock(spec=StringSession)
    mock_session.save.return_value = FAKE_SESSION_STR

    mock_me = MagicMock()
    mock_me.id = 987654321
    mock_me.username = "testuser"

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.sign_in = AsyncMock()
    mock_client.get_me = AsyncMock(return_value=mock_me)
    mock_client.disconnect = AsyncMock()
    mock_client.session = mock_session

    with patch.object(service, "_get_client", return_value=mock_client):
        updated_account = await service.verify_code(
            account.id, user.id, FAKE_PHONE, FAKE_CODE, FAKE_HASH,
            temp_session="fake_temp_session_string",
        )

    assert updated_account.status == TelegramAccountStatus.ACTIVE
    assert updated_account.session_ciphertext is not None
    # Ciphertext must differ from the plaintext session string (i.e. it's encrypted)
    assert updated_account.session_ciphertext != FAKE_SESSION_STR
    assert len(updated_account.session_ciphertext) > 10  # Fernet ciphertext is always long
    assert updated_account.telegram_user_id == 987654321

    get_settings.cache_clear()


async def test_verify_code_2fa(
    db_session: AsyncSession, user, account, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "testhash")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", valid_fernet_key())
    monkeypatch.setenv("BOT_TOKEN", "123456:test")
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")

    from app.core.config import get_settings
    get_settings.cache_clear()

    service = AccountService(db_session)

    mock_session = MagicMock(spec=StringSession)
    mock_session.save.return_value = FAKE_SESSION_STR

    mock_me = MagicMock()
    mock_me.id = 111
    mock_me.username = "twofa_user"

    call_count = {"n": 0}

    async def sign_in_side_effect(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise SessionPasswordNeededError(request=None)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.sign_in = AsyncMock(side_effect=sign_in_side_effect)
    mock_client.get_me = AsyncMock(return_value=mock_me)
    mock_client.disconnect = AsyncMock()
    mock_client.session = mock_session

    with patch.object(service, "_get_client", return_value=mock_client):
        updated = await service.verify_code(
            account.id, user.id, FAKE_PHONE, FAKE_CODE, FAKE_HASH,
            temp_session="fake_temp_session_string", password="mypassword"
        )

    assert updated.status == TelegramAccountStatus.ACTIVE
    # Verify password sign_in was called
    assert call_count["n"] == 2
    get_settings.cache_clear()


async def test_verify_code_wrong_code_raises(
    db_session: AsyncSession, user, account, monkeypatch
):
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "testhash")

    service = AccountService(db_session)

    mock_client = AsyncMock()
    mock_client.connect = AsyncMock()
    mock_client.sign_in = AsyncMock(side_effect=PhoneCodeInvalidError(request=None))
    mock_client.disconnect = AsyncMock()

    with patch.object(service, "_get_client", return_value=mock_client):
        with pytest.raises(AuthError, match="incorrect"):
            await service.verify_code(
                account.id, user.id, FAKE_PHONE, "99999", FAKE_HASH,
                temp_session="fake_temp_session_string",
            )


# ---------------------------------------------------------------------------
# load_session
# ---------------------------------------------------------------------------

async def test_load_session_decrypts_correctly(
    db_session: AsyncSession, user, account, monkeypatch
):
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", valid_fernet_key())
    monkeypatch.setenv("APP_SECRET_KEY", "test-secret")
    monkeypatch.setenv("TELEGRAM_API_ID", "12345")
    monkeypatch.setenv("TELEGRAM_API_HASH", "testhash")
    monkeypatch.setenv("BOT_TOKEN", "123456:test")

    from app.core.config import get_settings
    from app.core.crypto import SessionCipher
    get_settings.cache_clear()

    # Store an encrypted session
    cipher = SessionCipher.from_settings()
    ciphertext = cipher.encrypt(FAKE_SESSION_STR)

    repo = TelegramAccountRepository(db_session)
    await repo.update_session(account, session_ciphertext=ciphertext)

    service = AccountService(db_session)
    loaded = await service.load_session(account.id, user.id)

    assert isinstance(loaded, StringSession)
    assert loaded.save() == FAKE_SESSION_STR  # Decrypted correctly
    get_settings.cache_clear()


async def test_load_session_no_ciphertext_raises(
    db_session: AsyncSession, user, account
):
    service = AccountService(db_session)
    with pytest.raises(SessionError):
        await service.load_session(account.id, user.id)


async def test_load_session_wrong_user_raises(
    db_session: AsyncSession, user, account
):
    service = AccountService(db_session)
    with pytest.raises(AccountNotFoundError):
        await service.load_session(account.id, user_id=9999)


# ---------------------------------------------------------------------------
# disconnect_account
# ---------------------------------------------------------------------------

async def test_disconnect_account(db_session: AsyncSession, user, account):
    service = AccountService(db_session)
    result = await service.disconnect_account(account.id, user.id)
    assert result.status == TelegramAccountStatus.DISCONNECTED
