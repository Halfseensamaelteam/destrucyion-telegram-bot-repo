"""
tests/unit/services/test_nowpayments_capture.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
NOWPayments capture add-on identity: NPC- order ids on creation,
NP- retained for subscriptions, capture rows stored as capture_{plan}.
"""

import re
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.subscription import SubscriptionPlan
from app.services.nowpayments_subscription import (
    NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD,
    NOWPAYMENTS_PLAN_PRICES_USD,
    NOWPaymentsSubscriptionService,
    generate_nowpayments_order_id,
)

PROVIDER_PAYMENT = {
    "payment_id": 987654,
    "invoice_id": None,
    "pay_address": "TExampleAddress",
    "pay_amount": "12.34",
    "pay_currency": "usdttrc20",
    "payment_status": "waiting",
}


def _service_with_mock_provider() -> NOWPaymentsSubscriptionService:
    service = NOWPaymentsSubscriptionService.__new__(NOWPaymentsSubscriptionService)
    service.session = MagicMock()
    service.provider = MagicMock()
    service.provider.get_minimum_payment_amount = AsyncMock(
        return_value={"fiat_equivalent_usd": Decimal("0.50")}
    )
    service.provider.create_payment = AsyncMock(return_value=dict(PROVIDER_PAYMENT))
    service.repository = MagicMock()
    service.repository.create = AsyncMock(
        side_effect=lambda **kwargs: MagicMock(**kwargs)
    )
    return service


def test_nowpayments_order_id_formats():
    assert re.fullmatch(r"NP-[0-9a-f]{32}", generate_nowpayments_order_id())
    assert re.fullmatch(
        r"NP-[0-9a-f]{32}", generate_nowpayments_order_id("subscription")
    )
    assert re.fullmatch(
        r"NPC-[0-9a-f]{32}", generate_nowpayments_order_id("capture")
    )


def test_capture_usd_prices_are_half_of_subscription():
    for plan, price in NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD.items():
        assert price == NOWPAYMENTS_PLAN_PRICES_USD[plan] / 2


async def test_create_capture_payment_uses_npc_identity():
    service = _service_with_mock_provider()

    payment, _provider = await service.create_capture_payment(
        user_id=42,
        plan=SubscriptionPlan.MONTHLY,
        pay_currency="usdttrc20",
        outcome_currency="usdttrc20",
    )

    assert payment.order_id.startswith("NPC-")
    assert re.fullmatch(r"NPC-[0-9a-f]{32}", payment.order_id)
    assert payment.plan == "capture_monthly"
    assert payment.price_amount == NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD[
        SubscriptionPlan.MONTHLY
    ]

    # The provider received the same NPC- order id + capture description.
    create_kwargs = service.provider.create_payment.call_args.kwargs
    assert create_kwargs["order_id"] == payment.order_id
    assert "Auto-Capture" in create_kwargs["description"]


async def test_create_subscription_payment_keeps_np_identity():
    service = _service_with_mock_provider()

    payment, _provider = await service.create_payment(
        user_id=42,
        plan=SubscriptionPlan.WEEKLY,
        pay_currency="usdttrc20",
        outcome_currency="usdttrc20",
    )

    assert re.fullmatch(r"NP-[0-9a-f]{32}", payment.order_id)
    assert not payment.order_id.startswith("NPC-")
    assert payment.plan == "weekly"
