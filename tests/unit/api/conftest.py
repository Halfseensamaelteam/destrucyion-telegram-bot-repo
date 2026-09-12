"""
tests/unit/api/conftest.py
~~~~~~~~~~~~~~~~~~~~~~~~~~
Shared fixtures for API tests.

Uses FastAPI's dependency_overrides to inject the test SQLite session,
bypassing the real PostgreSQL database entirely.
"""

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import AsyncSession

from app.main import app
from app.api.deps import get_current_user, get_session
from app.db.models.user import User
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.media_record import MediaRecord, MediaRecordStatus, MediaType
from app.services.api_key import generate_api_key, hash_api_key


# ---------------------------------------------------------------------------
# HTTP client fixture with test DB override
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def api_client(db_session: AsyncSession):
    """AsyncClient for the FastAPI app with the test DB session injected."""

    async def override_get_session():
        yield db_session

    app.dependency_overrides[get_session] = override_get_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# User fixtures
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def user_a(db_session: AsyncSession) -> tuple[User, str]:
    """Create User A with an API key. Returns (user, raw_key)."""
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    user = User(
        telegram_user_id=1001,
        username="user_a",
        first_name="Alice",
        api_key_hash=key_hash,
        is_admin=False,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user, raw_key


@pytest_asyncio.fixture
async def user_b(db_session: AsyncSession) -> tuple[User, str]:
    """Create User B with an API key. Returns (user, raw_key)."""
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    user = User(
        telegram_user_id=2002,
        username="user_b",
        first_name="Bob",
        api_key_hash=key_hash,
        is_admin=False,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user, raw_key


@pytest_asyncio.fixture
async def admin_user(db_session: AsyncSession) -> tuple[User, str]:
    """Create an admin user with an API key."""
    raw_key = generate_api_key()
    key_hash = hash_api_key(raw_key)
    user = User(
        telegram_user_id=9999,
        username="admin",
        first_name="Admin",
        api_key_hash=key_hash,
        is_admin=True,
    )
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)
    return user, raw_key


@pytest_asyncio.fixture
async def account_for_user_a(db_session: AsyncSession, user_a) -> TelegramAccount:
    """A connected Telegram account belonging to User A."""
    user, _ = user_a
    account = TelegramAccount(
        user_id=user.id,
        phone_masked="+62****1234",
        status=TelegramAccountStatus.ACTIVE,
    )
    db_session.add(account)
    await db_session.flush()
    await db_session.refresh(account)
    return account


@pytest_asyncio.fixture
async def media_for_user_a(db_session: AsyncSession, user_a, account_for_user_a) -> MediaRecord:
    """A captured media record belonging to User A."""
    user, _ = user_a
    record = MediaRecord(
        user_id=user.id,
        telegram_account_id=account_for_user_a.id,
        source_chat_id=100,
        source_message_id=42,
        media_type=MediaType.PHOTO,
        status=MediaRecordStatus.SAVED,
    )
    db_session.add(record)
    await db_session.flush()
    await db_session.refresh(record)
    return record
