import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.db.models.subscription import SubscriptionPlan
from app.services.midtrans import MidtransService


pytestmark = pytest.mark.asyncio


def make_midtrans_response():
    return {
        "transaction_status": "pending",
        "transaction_id": "sandbox-tx-123",
        "qr_string": "000201010212TESTQR",
        "expiry_time": "2026-10-01 20:00:00",
        "actions": [
            {
                "name": "generate-qr-code",
                "url": "https://example.test/qr/sandbox-tx-123",
            }
        ],
    }


async def test_create_qris_transaction_uses_sandbox_qris_configuration():
    mock_client = MagicMock()
    mock_client.charge.return_value = make_midtrans_response()

    with patch(
        "app.services.midtrans.midtransclient.CoreApi",
        return_value=mock_client,
    ) as mock_core_api:
        service = MidtransService()

    mock_core_api.assert_called_once()
    _, kwargs = mock_core_api.call_args

    assert kwargs["is_production"] is False
    assert kwargs["server_key"]

    result = service.create_qris_transaction(
        user_id=1,
        plan=SubscriptionPlan.WEEKLY,
    )

    mock_client.charge.assert_called_once()

    parameter = mock_client.charge.call_args.args[0]

    assert parameter["payment_type"] == "qris"
    assert parameter["transaction_details"]["order_id"].startswith(
        "SUB-1-weekly-"
    )
    assert parameter["transaction_details"]["gross_amount"] > 0
    assert parameter["qris"] == {"acquirer": "gopay"}
    assert parameter["custom_expiry"] == {
        "expiry_duration": 15,
        "unit": "minute",
    }

    assert result["order_id"] == parameter["transaction_details"]["order_id"]
    assert result["plan"] == "weekly"
    assert result["currency"] == "IDR"
    assert result["status"] == "pending"
    assert result["transaction_id"] == "sandbox-tx-123"
    assert result["qr_string"] == "000201010212TESTQR"
    assert result["qr_image_url"] == (
        "https://example.test/qr/sandbox-tx-123"
    )

    assert result["expiry_time"] == datetime.datetime(
        2026,
        10,
        1,
        20,
        0,
        tzinfo=datetime.timezone(datetime.timedelta(hours=7)),
    )
