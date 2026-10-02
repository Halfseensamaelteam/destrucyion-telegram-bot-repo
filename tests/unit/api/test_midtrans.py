import hashlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.config import get_settings
from app.api.routes.midtrans import verify_midtrans_signature


pytestmark = pytest.mark.asyncio


def make_signature(order_id: str, status_code: str, gross_amount: str) -> str:
    server_key = get_settings().midtrans_server_key.get_secret_value()
    raw = f"{order_id}{status_code}{gross_amount}{server_key}"
    return hashlib.sha512(raw.encode("utf-8")).hexdigest()


def make_payload(
    *,
    order_id="SUB-1-weekly-TEST123",
    gross_amount="8000",
    transaction_status="pending",
    status_code="200",
):
    payload = {
        "order_id": order_id,
        "status_code": status_code,
        "gross_amount": gross_amount,
        "transaction_status": transaction_status,
        "transaction_id": "tx-123",
    }
    payload["signature_key"] = make_signature(
        order_id,
        status_code,
        gross_amount,
    )
    return payload


async def test_midtrans_signature_valid():
    payload = make_payload()

    assert verify_midtrans_signature(payload) is True


async def test_midtrans_signature_invalid():
    payload = make_payload()
    payload["signature_key"] = "invalid-signature"

    assert verify_midtrans_signature(payload) is False


async def test_midtrans_signature_rejects_missing_fields():
    payload = {
        "order_id": "SUB-1-weekly-TEST123",
        "status_code": "200",
        "gross_amount": "8000",
    }

    assert verify_midtrans_signature(payload) is False


async def test_webhook_invalid_signature_returns_401(api_client):
    payload = make_payload()
    payload["signature_key"] = "invalid-signature"

    response = await api_client.post(
        "/api/v1/midtrans/notification",
        json=payload,
    )

    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid Midtrans signature."


async def test_webhook_missing_order_id_returns_400(api_client):
    payload = make_payload()
    payload.pop("order_id")
    payload["signature_key"] = make_signature(
        "",
        payload["status_code"],
        payload["gross_amount"],
    )

    response = await api_client.post(
        "/api/v1/midtrans/notification",
        json=payload,
    )

    assert response.status_code == 401


async def test_webhook_payment_not_found_returns_404(api_client):
    payload = make_payload()

    with patch(
        "app.api.routes.midtrans.PaymentRepository"
    ) as mock_repo:
        mock_repo.return_value.get_by_order_id = AsyncMock(
            return_value=None
        )

        response = await api_client.post(
            "/api/v1/midtrans/notification",
            json=payload,
        )

    assert response.status_code == 404
    assert response.json()["detail"] == "Payment not found."


async def test_webhook_amount_mismatch_returns_400(api_client):
    payload = make_payload(gross_amount="9000")

    payment = MagicMock()
    payment.order_id = payload["order_id"]
    payment.amount = 8000
    payment.user_id = 1
    payment.status = MagicMock()

    with patch(
        "app.api.routes.midtrans.PaymentRepository"
    ) as mock_repo:
        mock_repo.return_value.get_by_order_id = AsyncMock(
            return_value=payment
        )

        response = await api_client.post(
            "/api/v1/midtrans/notification",
            json=payload,
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "Payment amount mismatch."
