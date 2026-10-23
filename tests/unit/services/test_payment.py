from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.payment import PaymentStatus
from app.db.models.subscription import SubscriptionPlan
from app.services.payment import PaymentService


pytestmark = pytest.mark.asyncio


def make_payment(status=PaymentStatus.PENDING):
    payment = MagicMock()
    payment.id = 1
    payment.user_id = 42
    payment.order_id = "SUB-42-weekly-TEST123"
    payment.transaction_id = "tx-123"
    payment.plan = SubscriptionPlan.WEEKLY.value
    payment.amount = 8000
    payment.currency = "IDR"
    payment.status = status
    return payment


async def test_settlement_grants_subscription_once():
    session = MagicMock()
    payment = make_payment(PaymentStatus.PENDING)
    subscription = MagicMock()

    payment_repo = MagicMock()
    payment_repo.get_by_order_id = AsyncMock(return_value=payment)
    payment_repo.update_transaction = AsyncMock()

    subscription_service = MagicMock()
    subscription_service.grant_subscription = AsyncMock(
        return_value=subscription
    )

    with patch(
        "app.services.payment.PaymentRepository",
        return_value=payment_repo,
    ), patch(
        "app.services.payment.MidtransService",
    ), patch(
        "app.services.payment.SubscriptionService",
        return_value=subscription_service,
    ):
        service = PaymentService(session)

        result_payment, result_subscription = (
            await service.process_notification(
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                status=PaymentStatus.SETTLEMENT,
            )
        )

    assert result_payment is payment
    assert result_subscription is subscription

    subscription_service.grant_subscription.assert_awaited_once_with(
        user_id=42,
        plan=SubscriptionPlan.WEEKLY,
    )


def make_capture_payment(status=PaymentStatus.PENDING):
    payment = make_payment(status)
    payment.order_id = "CAP-42-M-ABCDEF123456"
    payment.amount = 55000
    payment.plan = SubscriptionPlan.MONTHLY.value
    return payment


async def test_capture_settlement_grants_addon_not_subscription():
    session = MagicMock()
    payment = make_capture_payment(PaymentStatus.PENDING)
    capture_sub = MagicMock()

    payment_repo = MagicMock()
    payment_repo.get_by_order_id = AsyncMock(return_value=payment)
    payment_repo.update_transaction = AsyncMock()

    subscription_service = MagicMock()
    subscription_service.grant_subscription = AsyncMock()

    capture_service = MagicMock()
    capture_service.grant_capture_addon = AsyncMock(return_value=capture_sub)

    with patch(
        "app.services.payment.PaymentRepository",
        return_value=payment_repo,
    ), patch(
        "app.services.payment.MidtransService",
    ), patch(
        "app.services.payment.SubscriptionService",
        return_value=subscription_service,
    ), patch(
        "app.services.payment.CaptureAddonService",
        return_value=capture_service,
    ):
        service = PaymentService(session)

        result_payment, result_granted = await service.process_notification(
            order_id=payment.order_id,
            transaction_id=payment.transaction_id,
            status=PaymentStatus.SETTLEMENT,
        )

    assert result_payment is payment
    assert result_granted is capture_sub
    capture_service.grant_capture_addon.assert_awaited_once_with(
        user_id=42,
        plan=SubscriptionPlan.MONTHLY,
        order_id="CAP-42-M-ABCDEF123456",
    )
    subscription_service.grant_subscription.assert_not_awaited()


async def test_duplicate_capture_settlement_does_not_grant_again():
    session = MagicMock()
    payment = make_capture_payment(PaymentStatus.SETTLEMENT)

    payment_repo = MagicMock()
    payment_repo.get_by_order_id = AsyncMock(return_value=payment)
    payment_repo.update_transaction = AsyncMock()

    capture_service = MagicMock()
    capture_service.grant_capture_addon = AsyncMock()

    with patch(
        "app.services.payment.PaymentRepository",
        return_value=payment_repo,
    ), patch(
        "app.services.payment.MidtransService",
    ), patch(
        "app.services.payment.SubscriptionService",
    ), patch(
        "app.services.payment.CaptureAddonService",
        return_value=capture_service,
    ):
        service = PaymentService(session)

        result_payment, result_granted = await service.process_notification(
            order_id=payment.order_id,
            transaction_id=payment.transaction_id,
            status=PaymentStatus.SETTLEMENT,
        )

    assert result_payment is payment
    assert result_granted is None
    capture_service.grant_capture_addon.assert_not_awaited()


async def test_capture_order_id_format_and_prices():
    import re

    from app.core.config import get_settings
    from app.services.midtrans import MidtransError, MidtransService

    settings = get_settings()

    weekly = MidtransService.generate_capture_order_id(42, SubscriptionPlan.WEEKLY)
    monthly = MidtransService.generate_capture_order_id(42, SubscriptionPlan.MONTHLY)
    quarterly = MidtransService.generate_capture_order_id(7, SubscriptionPlan.QUARTERLY)

    assert re.fullmatch(r"CAP-42-W-[0-9A-F]{12}", weekly)
    assert re.fullmatch(r"CAP-42-M-[0-9A-F]{12}", monthly)
    assert re.fullmatch(r"CAP-7-Q-[0-9A-F]{12}", quarterly)
    # Payment Link's 36-char limit must hold for CAP- ids too.
    assert all(len(order_id) <= 36 for order_id in (weekly, monthly, quarterly))

    assert MidtransService._get_capture_price(SubscriptionPlan.WEEKLY) == settings.capture_weekly_price
    assert MidtransService._get_capture_price(SubscriptionPlan.MONTHLY) == settings.capture_monthly_price
    assert MidtransService._get_capture_price(SubscriptionPlan.QUARTERLY) == settings.capture_3_monthly_price

    with pytest.raises(MidtransError):
        MidtransService.generate_capture_order_id(42, SubscriptionPlan.LIFETIME)
    with pytest.raises(MidtransError):
        MidtransService._get_capture_price(SubscriptionPlan.LIFETIME)


async def test_duplicate_settlement_does_not_grant_subscription_again():
    session = MagicMock()
    payment = make_payment(PaymentStatus.SETTLEMENT)

    payment_repo = MagicMock()
    payment_repo.get_by_order_id = AsyncMock(return_value=payment)
    payment_repo.update_transaction = AsyncMock()

    subscription_service = MagicMock()
    subscription_service.grant_subscription = AsyncMock()

    with patch(
        "app.services.payment.PaymentRepository",
        return_value=payment_repo,
    ), patch(
        "app.services.payment.MidtransService",
    ), patch(
        "app.services.payment.SubscriptionService",
        return_value=subscription_service,
    ):
        service = PaymentService(session)

        result_payment, result_subscription = (
            await service.process_notification(
                order_id=payment.order_id,
                transaction_id=payment.transaction_id,
                status=PaymentStatus.SETTLEMENT,
            )
        )

    assert result_payment is payment
    assert result_subscription is None

    subscription_service.grant_subscription.assert_not_awaited()
