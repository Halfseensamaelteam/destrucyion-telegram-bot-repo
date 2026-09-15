"""
tests/integration/test_postgres_enums.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for PostgreSQL enum columns.

These tests verify that enum values are correctly stored and retrieved
from PostgreSQL, ensuring the values_callable fix works properly.

These tests require a real PostgreSQL database connection. They will be
skipped if DATABASE_URL is not set or if it points to SQLite.

Run with:
    DATABASE_URL="postgresql+asyncpg://user:pass@localhost/db" uv run pytest tests/integration/test_postgres_enums.py -v
"""

import os
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

from app.core.config import get_settings
from app.db.models.base import Base
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.media_record import MediaRecord, MediaType, MediaRecordStatus
from app.db.models.subscription import Subscription, SubscriptionPlan, SubscriptionStatus
from app.db.models.user import User


@pytest.fixture(scope="module")
def is_postgres():
    """Skip tests if not running against PostgreSQL."""
    settings = get_settings()
    if not settings.database_url.startswith("postgresql"):
        pytest.skip("PostgreSQL integration tests require DATABASE_URL to point to PostgreSQL")
    return True


@pytest_asyncio.fixture(scope="module")
async def postgres_engine(is_postgres):
    """Async engine for PostgreSQL integration tests.
    
    Uses the real DATABASE_URL from environment.
    Creates all tables before tests, drops them after.
    """
    settings = get_settings()
    engine = create_async_engine(
        settings.database_url,
        echo=False,
    )
    
    # Create all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    
    yield engine
    
    # Drop all tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    
    await engine.dispose()


@pytest_asyncio.fixture
async def postgres_session(postgres_engine):
    """Async session bound to PostgreSQL test engine."""
    factory = async_sessionmaker(
        bind=postgres_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )
    async with factory() as session:
        yield session
        await session.rollback()


class TestTelegramAccountStatusEnum:
    """Test TelegramAccountStatus enum with PostgreSQL."""

    @pytest.mark.asyncio
    async def test_create_account_with_active_status(self, postgres_session):
        """Test creating an account with ACTIVE status."""
        user = User(
            telegram_user_id=1001,
            username="test_user",
            api_key_hash="abc123",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****1234",
            status=TelegramAccountStatus.ACTIVE,
        )
        postgres_session.add(account)
        await postgres_session.commit()
        await postgres_session.refresh(account)

        assert account.status == TelegramAccountStatus.ACTIVE
        assert account.status.value == "active"

    @pytest.mark.asyncio
    async def test_create_account_with_paused_status(self, postgres_session):
        """Test creating an account with PAUSED status."""
        user = User(
            telegram_user_id=1002,
            username="test_user2",
            api_key_hash="def456",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****5678",
            status=TelegramAccountStatus.PAUSED,
        )
        postgres_session.add(account)
        await postgres_session.commit()
        await postgres_session.refresh(account)

        assert account.status == TelegramAccountStatus.PAUSED
        assert account.status.value == "paused"

    @pytest.mark.asyncio
    async def test_update_account_status(self, postgres_session):
        """Test updating account status."""
        user = User(
            telegram_user_id=1003,
            username="test_user3",
            api_key_hash="ghi789",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****9012",
            status=TelegramAccountStatus.DISCONNECTED,
        )
        postgres_session.add(account)
        await postgres_session.commit()
        await postgres_session.refresh(account)

        # Update status
        account.status = TelegramAccountStatus.ERROR
        await postgres_session.commit()
        await postgres_session.refresh(account)

        assert account.status == TelegramAccountStatus.ERROR
        assert account.status.value == "error"


class TestMediaTypeEnum:
    """Test MediaType enum with PostgreSQL."""

    @pytest.mark.asyncio
    async def test_create_media_record_with_photo_type(self, postgres_session):
        """Test creating a media record with PHOTO type."""
        user = User(
            telegram_user_id=2001,
            username="media_user",
            api_key_hash="xyz123",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****1111",
            status=TelegramAccountStatus.ACTIVE,
        )
        postgres_session.add(account)
        await postgres_session.flush()

        media = MediaRecord(
            user_id=user.id,
            telegram_account_id=account.id,
            source_chat_id=100,
            source_message_id=42,
            media_type=MediaType.PHOTO,
            status=MediaRecordStatus.SAVED,
        )
        postgres_session.add(media)
        await postgres_session.commit()
        await postgres_session.refresh(media)

        assert media.media_type == MediaType.PHOTO
        assert media.media_type.value == "photo"

    @pytest.mark.asyncio
    async def test_create_media_record_with_video_type(self, postgres_session):
        """Test creating a media record with VIDEO type."""
        user = User(
            telegram_user_id=2002,
            username="media_user2",
            api_key_hash="xyz456",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****2222",
            status=TelegramAccountStatus.ACTIVE,
        )
        postgres_session.add(account)
        await postgres_session.flush()

        media = MediaRecord(
            user_id=user.id,
            telegram_account_id=account.id,
            source_chat_id=200,
            source_message_id=43,
            media_type=MediaType.VIDEO,
            status=MediaRecordStatus.SAVED,
        )
        postgres_session.add(media)
        await postgres_session.commit()
        await postgres_session.refresh(media)

        assert media.media_type == MediaType.VIDEO
        assert media.media_type.value == "video"


class TestMediaRecordStatusEnum:
    """Test MediaRecordStatus enum with PostgreSQL."""

    @pytest.mark.asyncio
    async def test_create_media_record_with_pending_status(self, postgres_session):
        """Test creating a media record with PENDING status."""
        user = User(
            telegram_user_id=3001,
            username="status_user",
            api_key_hash="abc999",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****3333",
            status=TelegramAccountStatus.ACTIVE,
        )
        postgres_session.add(account)
        await postgres_session.flush()

        media = MediaRecord(
            user_id=user.id,
            telegram_account_id=account.id,
            source_chat_id=300,
            source_message_id=44,
            media_type=MediaType.PHOTO,
            status=MediaRecordStatus.PENDING,
        )
        postgres_session.add(media)
        await postgres_session.commit()
        await postgres_session.refresh(media)

        assert media.status == MediaRecordStatus.PENDING
        assert media.status.value == "pending"

    @pytest.mark.asyncio
    async def test_update_media_record_status(self, postgres_session):
        """Test updating media record status."""
        user = User(
            telegram_user_id=3002,
            username="status_user2",
            api_key_hash="def999",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****4444",
            status=TelegramAccountStatus.ACTIVE,
        )
        postgres_session.add(account)
        await postgres_session.flush()

        media = MediaRecord(
            user_id=user.id,
            telegram_account_id=account.id,
            source_chat_id=400,
            source_message_id=45,
            media_type=MediaType.PHOTO,
            status=MediaRecordStatus.PENDING,
        )
        postgres_session.add(media)
        await postgres_session.commit()
        await postgres_session.refresh(media)

        # Update status
        media.status = MediaRecordStatus.SAVED
        await postgres_session.commit()
        await postgres_session.refresh(media)

        assert media.status == MediaRecordStatus.SAVED
        assert media.status.value == "saved"


class TestSubscriptionPlanEnum:
    """Test SubscriptionPlan enum with PostgreSQL."""

    @pytest.mark.asyncio
    async def test_create_subscription_with_monthly_plan(self, postgres_session):
        """Test creating a subscription with MONTHLY plan."""
        user = User(
            telegram_user_id=4001,
            username="sub_user",
            api_key_hash="sub123",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        from datetime import datetime, timezone

        subscription = Subscription(
            user_id=user.id,
            plan=SubscriptionPlan.MONTHLY,
            starts_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc),
            status=SubscriptionStatus.ACTIVE,
        )
        postgres_session.add(subscription)
        await postgres_session.commit()
        await postgres_session.refresh(subscription)

        assert subscription.plan == SubscriptionPlan.MONTHLY
        assert subscription.plan.value == "monthly"

    @pytest.mark.asyncio
    async def test_create_subscription_with_lifetime_plan(self, postgres_session):
        """Test creating a subscription with LIFETIME plan."""
        user = User(
            telegram_user_id=4002,
            username="sub_user2",
            api_key_hash="sub456",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        from datetime import datetime, timezone

        subscription = Subscription(
            user_id=user.id,
            plan=SubscriptionPlan.LIFETIME,
            starts_at=datetime.now(timezone.utc),
            expires_at=None,
            status=SubscriptionStatus.ACTIVE,
        )
        postgres_session.add(subscription)
        await postgres_session.commit()
        await postgres_session.refresh(subscription)

        assert subscription.plan == SubscriptionPlan.LIFETIME
        assert subscription.plan.value == "lifetime"


class TestSubscriptionStatusEnum:
    """Test SubscriptionStatus enum with PostgreSQL."""

    @pytest.mark.asyncio
    async def test_create_subscription_with_active_status(self, postgres_session):
        """Test creating a subscription with ACTIVE status."""
        user = User(
            telegram_user_id=5001,
            username="status_sub_user",
            api_key_hash="status123",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        from datetime import datetime, timezone

        subscription = Subscription(
            user_id=user.id,
            plan=SubscriptionPlan.MONTHLY,
            starts_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc),
            status=SubscriptionStatus.ACTIVE,
        )
        postgres_session.add(subscription)
        await postgres_session.commit()
        await postgres_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.ACTIVE
        assert subscription.status.value == "active"

    @pytest.mark.asyncio
    async def test_update_subscription_status(self, postgres_session):
        """Test updating subscription status."""
        user = User(
            telegram_user_id=5002,
            username="status_sub_user2",
            api_key_hash="status456",
        )
        postgres_session.add(user)
        await postgres_session.flush()

        from datetime import datetime, timezone

        subscription = Subscription(
            user_id=user.id,
            plan=SubscriptionPlan.MONTHLY,
            starts_at=datetime.now(timezone.utc),
            expires_at=datetime.now(timezone.utc),
            status=SubscriptionStatus.ACTIVE,
        )
        postgres_session.add(subscription)
        await postgres_session.commit()
        await postgres_session.refresh(subscription)

        # Update status
        subscription.status = SubscriptionStatus.EXPIRED
        await postgres_session.commit()
        await postgres_session.refresh(subscription)

        assert subscription.status == SubscriptionStatus.EXPIRED
        assert subscription.status.value == "expired"
