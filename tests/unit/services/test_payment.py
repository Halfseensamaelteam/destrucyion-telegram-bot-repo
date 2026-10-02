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
