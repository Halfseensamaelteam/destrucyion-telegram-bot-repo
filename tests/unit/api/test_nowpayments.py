"""
tests/unit/api/test_nowpayments.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
NOWPayments IPN: NPC- orders activate the capture add-on (never a base
subscription); NP- orders keep activating subscriptions.
"""

from contextlib import asynccontextmanager
from datetime import timedelta
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.routes.nowpayments as np_routes
from app.db.models.capture import CaptureSubscription
from app.db.models.nowpayments_payment import NOWPaymentsPayment
from app.db.models.subscription import Subscription
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


@pytest.fixture
def ipn_env(monkeypatch):
    monkeypatch.setenv("NOWPAYMENTS_SANDBOX_IPN_SECRET", "test-ipn-secret")
    monkeypatch.setenv("NOWPAYMENTS_IPN_SECRET", "test-ipn-secret")


def _make_factory(db_engine):
    maker = async_sessionmaker(
        bind=db_engine, class_=AsyncSession, expire_on_commit=False, autoflush=False
    )

    @asynccontextmanager
    async def factory():
        async with maker() as session:
            yield session

    return factory


async def _seed(db_engine, *, order_id: str, plan: str, price: str):
    factory = _make_factory(db_engine)
    async with factory() as session:
        user = User(telegram_user_id=4242, username="npuser", first_name="Np")
        session.add(user)
        await session.flush()
        payment = NOWPaymentsPayment(
            user_id=user.id,
            order_id=order_id,
            plan=plan,
            price_amount=Decimal(price),
            price_currency="usd",
            pay_currency="usdttrc20",
            outcome_currency="usdttrc20",
            provider_payment_id="777001",
            invoice_id=None,
            pay_address="TExampleAddress",
            pay_amount=Decimal("10.5"),
            payment_status="waiting",
        )
        session.add(payment)
        await session.commit()
        return user.id


def _payload(order_id: str, price: str) -> dict:
    return {
        "payment_id": "777001",
        "order_id": order_id,
        "payment_status": "finished",
        "price_amount": price,
        "price_currency": "usd",
        "pay_currency": "usdttrc20",
        "pay_amount": "10.5",
        "outcome_currency": "usdttrc20",
    }


async def _post_ipn(api_client, db_engine, payload):
    factory = _make_factory(db_engine)
    notifier_instance = MagicMock()
    notifier_instance.send_capture_activated = AsyncMock()
    notifier_instance.send_subscription_activated = AsyncMock()

    with patch.object(np_routes, "_get_session_factory", return_value=factory), \
         patch.object(
             np_routes.NOWPaymentsService,
             "verify_ipn_signature",
             staticmethod(lambda payload, signature, secret: True),
         ), \
         patch.object(
             np_routes, "TelegramNotifier", return_value=notifier_instance
         ):
        response = await api_client.post(
            "/api/v1/nowpayments/notification",
            json=payload,
            headers={"x-nowpayments-sig": "test-signature"},
        )
    return response, notifier_instance


async def test_npc_ipn_finished_activates_capture_addon(api_client, db_engine, ipn_env):
    order_id = "NPC-cae85fabfd4f45fc81f9bb71062fc5d4"
    user_id = await _seed(db_engine, order_id=order_id, plan="capture_monthly", price="12.00")

    response, notifier = await _post_ipn(api_client, db_engine, _payload(order_id, "12.00"))

    assert response.status_code == 200
    assert response.json()["payment_status"] == "finished"

    factory = _make_factory(db_engine)
    async with factory() as session:
        capture_sub = (
            await session.execute(
                select(CaptureSubscription).where(
                    CaptureSubscription.user_id == user_id
                )
            )
        ).scalar_one_or_none()
        base_sub = (
            await session.execute(
                select(Subscription).where(Subscription.user_id == user_id)
            )
        ).scalar_one_or_none()
        payment = (
            await session.execute(
                select(NOWPaymentsPayment).where(
                    NOWPaymentsPayment.order_id == order_id
                )
            )
        ).scalar_one()

    assert capture_sub is not None
    assert capture_sub.plan.value == "monthly"
    assert capture_sub.is_active
    assert capture_sub.last_order_id == order_id
    assert capture_sub.expires_at - capture_sub.starts_at <= timedelta(days=30)
    assert base_sub is None
    assert payment.activated_at is not None

    notifier.send_capture_activated.assert_awaited_once()
    notifier.send_subscription_activated.assert_not_awaited()


async def test_np_ipn_finished_still_activates_subscription(api_client, db_engine, ipn_env):
    order_id = "NP-cae85fabfd4f45fc81f9bb71062fc5d4"
    user_id = await _seed(db_engine, order_id=order_id, plan="weekly", price="6.00")

    response, notifier = await _post_ipn(api_client, db_engine, _payload(order_id, "6.00"))

    assert response.status_code == 200

    factory = _make_factory(db_engine)
    async with factory() as session:
        base_sub = (
            await session.execute(
                select(Subscription).where(Subscription.user_id == user_id)
            )
        ).scalar_one_or_none()
        capture_sub = (
            await session.execute(
                select(CaptureSubscription).where(
                    CaptureSubscription.user_id == user_id
                )
            )
        ).scalar_one_or_none()

    assert base_sub is not None
    assert capture_sub is None
    notifier.send_subscription_activated.assert_awaited_once()
    notifier.send_capture_activated.assert_not_awaited()
