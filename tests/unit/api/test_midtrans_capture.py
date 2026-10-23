"""
tests/unit/api/test_midtrans_capture.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Midtrans webhook: a settled CAP- order confirms the capture add-on
(send_capture_activated), never the base subscription message.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

pytestmark = pytest.mark.asyncio


def _payload(order_id: str, gross_amount: str) -> dict:
    return {
        "order_id": order_id,
        "status_code": "200",
        "gross_amount": gross_amount,
        "transaction_status": "settlement",
        "transaction_id": "tx-cap-1",
        "signature_key": "patched-out",
    }


async def _post(api_client, *, order_id: str, amount: int):
    payload = _payload(order_id, str(amount))

    payment = MagicMock()
    payment.order_id = order_id
    payment.amount = amount
    payment.plan = "monthly"
    payment.user_id = 42
    from app.db.models.payment import PaymentStatus

    payment.status = PaymentStatus.PENDING

    granted = MagicMock()
    granted.expires_at = None

    notifier_instance = MagicMock()
    notifier_instance.send_capture_activated = AsyncMock()
    notifier_instance.send_subscription_activated = AsyncMock()

    with patch(
        "app.api.routes.midtrans.verify_midtrans_signature",
        return_value=True,
    ), patch(
        "app.api.routes.midtrans.PaymentRepository"
    ) as mock_repo, patch(
        "app.api.routes.midtrans.PaymentService"
    ) as mock_service, patch(
        "app.api.routes.midtrans.UserRepository"
    ) as mock_user_repo, patch(
        "app.api.routes.midtrans.TelegramNotifier",
        return_value=notifier_instance,
    ):
        mock_repo.return_value.get_by_order_id = AsyncMock(return_value=payment)
        mock_service.return_value.process_notification = AsyncMock(
            return_value=(payment, granted)
        )
        mock_user_repo.return_value.get_by_id = AsyncMock(
            return_value=MagicMock(telegram_user_id=123456)
        )

        response = await api_client.post(
            "/api/v1/midtrans/notification",
            json=payload,
        )

    return response, notifier_instance


async def test_cap_settlement_notifies_capture_activation(api_client):
    response, notifier = await _post(
        api_client, order_id="CAP-42-M-ABCDEF123456", amount=55000
    )

    assert response.status_code == 200
    notifier.send_capture_activated.assert_awaited_once()
    kwargs = notifier.send_capture_activated.call_args.kwargs
    assert kwargs["telegram_user_id"] == 123456
    assert kwargs["plan"] == "monthly"
    notifier.send_subscription_activated.assert_not_awaited()


async def test_sub_settlement_still_notifies_subscription(api_client):
    response, notifier = await _post(
        api_client, order_id="SUB-42-monthly-ABCDEF123456", amount=30000
    )

    assert response.status_code == 200
    notifier.send_subscription_activated.assert_awaited_once()
    notifier.send_capture_activated.assert_not_awaited()
