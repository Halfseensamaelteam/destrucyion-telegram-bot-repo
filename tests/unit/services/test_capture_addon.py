"""
tests/unit/services/test_capture_addon.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
CaptureAddonService entitlement/registration semantics and the
worker-side CaptureGate against a real (SQLite) database.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.capture import CaptureChannel, CaptureSubscription
from app.db.models.subscription import (
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.user import User
from app.services.capture_addon import (
    CaptureAddonError,
    CaptureAddonService,
    CaptureGate,
)

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """SQLite returns naive datetimes; normalize for arithmetic."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _make_user(session: AsyncSession, telegram_id: int = 555) -> User:
    user = User(telegram_user_id=telegram_id, username="cap", first_name="Cap")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _make_account(session: AsyncSession, user: User) -> TelegramAccount:
    account = TelegramAccount(
        user_id=user.id,
        phone_masked="+62****9999",
        status=TelegramAccountStatus.ACTIVE,
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
    return account


# ----------------------------------------------------------------------
# Entitlement: grant / extend / restart
# ----------------------------------------------------------------------

async def test_grant_creates_active_addon(db_session: AsyncSession):
    user = await _make_user(db_session)
    service = CaptureAddonService(db_session)

    granted = await service.grant_capture_addon(
        user_id=user.id, plan=SubscriptionPlan.MONTHLY, order_id="CAP-1-M-ABC"
    )

    assert granted.plan == SubscriptionPlan.MONTHLY
    assert granted.status == SubscriptionStatus.ACTIVE
    assert granted.last_order_id == "CAP-1-M-ABC"
    assert granted.expires_at is not None
    delta = granted.expires_at - granted.starts_at
    assert timedelta(days=29) < delta <= timedelta(days=30)
    assert await service.check_active(user.id) is True


async def test_grant_weekly_and_quarterly_durations(db_session: AsyncSession):
    user = await _make_user(db_session)
    service = CaptureAddonService(db_session)

    weekly = await service.grant_capture_addon(
        user_id=user.id, plan=SubscriptionPlan.WEEKLY
    )
    assert timedelta(days=6) < weekly.expires_at - weekly.starts_at <= timedelta(days=7)

    other = await _make_user(db_session, telegram_id=556)
    quarterly = await service.grant_capture_addon(
        user_id=other.id, plan=SubscriptionPlan.QUARTERLY
    )
    assert timedelta(days=89) < quarterly.expires_at - quarterly.starts_at <= timedelta(days=90)


async def test_active_addon_extends_from_existing_expiry(db_session: AsyncSession):
    user = await _make_user(db_session)
    existing = CaptureSubscription(
        user_id=user.id,
        plan=SubscriptionPlan.WEEKLY,
        starts_at=_now() - timedelta(days=2),
        expires_at=_now() + timedelta(days=5),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add(existing)
    await db_session.flush()

    service = CaptureAddonService(db_session)
    granted = await service.grant_capture_addon(
        user_id=user.id, plan=SubscriptionPlan.WEEKLY, order_id="CAP-9-W-XYZ"
    )

    # 5 days remaining + 7 new days ≈ 12 days from now (not 7 from now).
    remaining = _aware(granted.expires_at) - _now()
    assert timedelta(days=11) < remaining <= timedelta(days=12)
    assert granted.last_order_id == "CAP-9-W-XYZ"


async def test_expired_addon_restarts_from_now(db_session: AsyncSession):
    user = await _make_user(db_session)
    existing = CaptureSubscription(
        user_id=user.id,
        plan=SubscriptionPlan.MONTHLY,
        starts_at=_now() - timedelta(days=60),
        expires_at=_now() - timedelta(days=30),
        status=SubscriptionStatus.ACTIVE,
    )
    db_session.add(existing)
    await db_session.flush()

    service = CaptureAddonService(db_session)
    assert await service.check_active(user.id) is False

    granted = await service.grant_capture_addon(
        user_id=user.id, plan=SubscriptionPlan.WEEKLY
    )
    remaining = _aware(granted.expires_at) - _now()
    assert timedelta(days=6) < remaining <= timedelta(days=7)
    assert await service.check_active(user.id) is True


async def test_grant_lifetime_is_rejected(db_session: AsyncSession):
    user = await _make_user(db_session)
    service = CaptureAddonService(db_session)
    with pytest.raises(ValueError):
        await service.grant_capture_addon(
            user_id=user.id, plan=SubscriptionPlan.LIFETIME
        )


async def test_addon_is_independent_from_base_subscription(db_session: AsyncSession):
    """Granting the add-on must not create/alter a base subscription."""
    user = await _make_user(db_session)
    service = CaptureAddonService(db_session)
    await service.grant_capture_addon(
        user_id=user.id, plan=SubscriptionPlan.WEEKLY
    )
    await db_session.flush()

    from sqlalchemy import select

    result = await db_session.execute(
        select(Subscription).where(Subscription.user_id == user.id)
    )
    assert result.scalar_one_or_none() is None


# ----------------------------------------------------------------------
# Registration
# ----------------------------------------------------------------------

async def test_register_and_remove_channel(db_session: AsyncSession):
    user = await _make_user(db_session)
    account = await _make_account(db_session, user)
    service = CaptureAddonService(db_session)

    registration = await service.register_channel(account.id, -1001234567890)
    assert registration.chat_id == -1001234567890

    fetched = await service.get_registered_channel(account.id)
    assert fetched is not None and fetched.chat_id == -1001234567890

    with pytest.raises(CaptureAddonError):
        await service.register_channel(account.id, -1009999999999)

    assert await service.remove_channel(account.id) is True
    assert await service.get_registered_channel(account.id) is None
    assert await service.remove_channel(account.id) is False


# ----------------------------------------------------------------------
# CaptureGate (worker side, real DB + factory)
# ----------------------------------------------------------------------

def _factory(db_engine) -> async_sessionmaker:
    return async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )


async def _seed_gate_state(factory, *, with_registration: bool, addon_expires: datetime | None):
    async with factory() as session:
        user = User(telegram_user_id=777, username="gate", first_name="Gate")
        session.add(user)
        await session.flush()
        account = TelegramAccount(
            user_id=user.id,
            phone_masked="+62****1111",
            status=TelegramAccountStatus.ACTIVE,
        )
        session.add(account)
        await session.flush()
        if with_registration:
            session.add(
                CaptureChannel(telegram_account_id=account.id, chat_id=-100123)
            )
        if addon_expires is not None:
            session.add(
                CaptureSubscription(
                    user_id=user.id,
                    plan=SubscriptionPlan.MONTHLY,
                    starts_at=_now() - timedelta(days=1),
                    expires_at=addon_expires,
                    status=SubscriptionStatus.ACTIVE,
                )
            )
        await session.commit()
        return account.id


async def test_gate_allows_registered_channel_with_active_addon(db_engine):
    factory = _factory(db_engine)
    account_id = await _seed_gate_state(
        factory, with_registration=True, addon_expires=_now() + timedelta(days=10)
    )
    gate = CaptureGate(factory)

    assert await gate.is_allowed(account_id=account_id, chat_id=-100123) is True
    assert await gate.is_allowed(account_id=account_id, chat_id=-100999) is False


async def test_gate_denies_without_registration(db_engine):
    factory = _factory(db_engine)
    account_id = await _seed_gate_state(
        factory, with_registration=False, addon_expires=_now() + timedelta(days=10)
    )
    gate = CaptureGate(factory)
    assert await gate.is_allowed(account_id=account_id, chat_id=-100123) is False


async def test_gate_denies_when_addon_expired(db_engine):
    factory = _factory(db_engine)
    account_id = await _seed_gate_state(
        factory, with_registration=True, addon_expires=_now() - timedelta(days=1)
    )
    gate = CaptureGate(factory)
    assert await gate.is_allowed(account_id=account_id, chat_id=-100123) is False


async def test_gate_caches_within_ttl(db_engine):
    factory = _factory(db_engine)
    account_id = await _seed_gate_state(
        factory, with_registration=True, addon_expires=_now() + timedelta(days=10)
    )
    gate = CaptureGate(factory)
    assert await gate.is_allowed(account_id=account_id, chat_id=-100123) is True

    # Revoke everything in the DB: within the TTL the cached answer wins.
    async with factory() as session:
        from sqlalchemy import delete, select

        user_id = (
            await session.execute(
                select(TelegramAccount.user_id).where(
                    TelegramAccount.id == account_id
                )
            )
        ).scalar_one()
        await session.execute(
            delete(CaptureChannel).where(
                CaptureChannel.telegram_account_id == account_id
            )
        )
        await session.execute(
            delete(CaptureSubscription).where(
                CaptureSubscription.user_id == user_id
            )
        )
        await session.commit()

    assert await gate.is_allowed(account_id=account_id, chat_id=-100123) is True

    # Force cache expiry -> the gate re-reads the DB and now denies.
    gate._cache[account_id] = (0.0, -100123, True)
    assert await gate.is_allowed(account_id=account_id, chat_id=-100123) is False
