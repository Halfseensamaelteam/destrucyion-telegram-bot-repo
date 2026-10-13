import pytest
from datetime import datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SubscriptionPlan, SubscriptionStatus
from app.db.repositories import UserRepository
from app.services.subscription import SubscriptionError, SubscriptionService

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def user(db_session: AsyncSession):
    repo = UserRepository(db_session)
    return await repo.create(telegram_user_id=999)


async def test_grant_subscription(db_session: AsyncSession, user):
    service = SubscriptionService(db_session)
    
    # Grant new
    sub = await service.grant_subscription(user.id, SubscriptionPlan.WEEKLY)
    assert sub.id is not None
    assert sub.plan == SubscriptionPlan.WEEKLY
    assert sub.status == SubscriptionStatus.ACTIVE
    assert sub.expires_at is not None
    
    # Check active
    assert await service.check_active(user.id) is True
    
    # Grant again (overwrite)
    now = datetime.now(timezone.utc)
    sub2 = await service.grant_subscription(
        user.id, SubscriptionPlan.LIFETIME, starts_at=now
    )
    assert sub2.id == sub.id
    assert sub2.plan == SubscriptionPlan.LIFETIME
    assert sub2.expires_at is None
    assert await service.check_active(user.id) is True


async def test_renew_subscription_active(db_session: AsyncSession, user):
    service = SubscriptionService(db_session)
    
    # Grant weekly
    now = datetime.now(timezone.utc)
    sub = await service.grant_subscription(
        user.id, SubscriptionPlan.WEEKLY, starts_at=now
    )
    original_expires = sub.expires_at
    if original_expires.tzinfo is None:
        original_expires = original_expires.replace(tzinfo=timezone.utc)
    
    # Renew
    renewed = await service.renew_subscription(user.id)
    
    # Should be appended
    renewed_expires = renewed.expires_at
    if renewed_expires.tzinfo is None:
        renewed_expires = renewed_expires.replace(tzinfo=timezone.utc)
        
    diff = renewed_expires - original_expires
    assert diff.days == 7


async def test_renew_subscription_expired(db_session: AsyncSession, user):
    service = SubscriptionService(db_session)
    
    # Grant an already expired subscription
    now = datetime.now(timezone.utc)
    past = now - timedelta(days=10)
    
    sub = await service.grant_subscription(
        user.id, SubscriptionPlan.WEEKLY, starts_at=past
    )
    assert await service.check_active(user.id) is False
    
    # Renew should start from now
    renewed = await service.renew_subscription(user.id)
    assert await service.check_active(user.id) is True
    
    renewed_expires = renewed.expires_at
    if renewed_expires.tzinfo is None:
        renewed_expires = renewed_expires.replace(tzinfo=timezone.utc)
        
    assert renewed_expires > now + timedelta(days=6)


async def test_revoke_subscription(db_session: AsyncSession, user):
    service = SubscriptionService(db_session)
    
    # Grant
    await service.grant_subscription(user.id, SubscriptionPlan.MONTHLY)
    assert await service.check_active(user.id) is True
    
    # Revoke
    revoked = await service.revoke_subscription(user.id)
    assert revoked.status == SubscriptionStatus.CANCELLED
    assert await service.check_active(user.id) is False


async def test_renew_no_subscription_raises(db_session: AsyncSession, user):
    service = SubscriptionService(db_session)
    with pytest.raises(SubscriptionError):
        await service.renew_subscription(user.id)


async def test_quarterly_subscription_expires_after_90_days(db_session: AsyncSession, user):
    service = SubscriptionService(db_session)
    start = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)

    sub = await service.grant_subscription(
        user.id, SubscriptionPlan.QUARTERLY, starts_at=start
    )

    assert sub.plan == SubscriptionPlan.QUARTERLY
    assert sub.expires_at.replace(tzinfo=timezone.utc) == start + timedelta(days=90)