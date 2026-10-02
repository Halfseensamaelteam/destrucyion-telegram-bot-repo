"""
app.services.midtrans
~~~~~~~~~~~~~~~~~~~~~
Midtrans payment gateway integration.
"""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import midtransclient

from app.core.config import get_settings
from app.db.models import SubscriptionPlan


class MidtransError(Exception):
    """Base exception for Midtrans-related errors."""


class MidtransService:
    """Service layer for Midtrans payment operations."""

    def __init__(self) -> None:
        settings = get_settings()

        server_key = settings.midtrans_server_key.get_secret_value()
        if not server_key:
            raise MidtransError("Midtrans Server Key is not configured.")

        self._client = midtransclient.CoreApi(
            is_production=settings.midtrans_is_production,
            server_key=server_key,
        )

    @staticmethod
    def _get_price(plan: SubscriptionPlan) -> int:
        """Return the configured price for a subscription plan."""
        settings = get_settings()

        prices = {
            SubscriptionPlan.WEEKLY: settings.subscription_weekly_price,
            SubscriptionPlan.MONTHLY: settings.subscription_monthly_price,
            SubscriptionPlan.LIFETIME: settings.subscription_lifetime_price,
        }

        try:
            return prices[plan]
        except KeyError as exc:
            raise MidtransError(
                f"Unsupported subscription plan: {plan}"
            ) from exc

    @staticmethod
    def generate_order_id(user_id: int, plan: SubscriptionPlan) -> str:
        """Generate a unique Midtrans order ID."""
        return f"SUB-{user_id}-{plan.value}-{uuid4().hex[:12].upper()}"

    @staticmethod
    def _parse_expiry_time(
        expiry_time: str | None,
    ) -> datetime | None:
        """Parse Midtrans expiry time as an aware WIB datetime."""

        if not expiry_time:
            return None

        wib = timezone(timedelta(hours=7))

        try:
            return datetime.strptime(
                expiry_time,
                "%Y-%m-%d %H:%M:%S",
            ).replace(tzinfo=wib)
        except ValueError as exc:
            raise MidtransError(
                f"Invalid Midtrans expiry_time: {expiry_time}"
            ) from exc

    def create_qris_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> dict:
        """Create a Midtrans transaction for a subscription purchase."""
        amount = self._get_price(plan)
        order_id = self.generate_order_id(user_id, plan)

        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")

        parameter = {
            "payment_type": "qris",
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "qris": {
                "acquirer": "gopay",
            },
            "custom_expiry": {
                "expiry_duration": 15,
                "unit": "minute",
            },
        }

        try:
            response = self._client.charge(parameter)
        except Exception as exc:
            raise MidtransError(
                f"Failed to create Midtrans transaction: {exc}"
            ) from exc

        actions = response.get("actions") or []

        qr_image_url = next(
            (
                action.get("url")
                for action in actions
                if action.get("name") == "generate-qr-code"
            ),
            None,
        )

        expiry_time = self._parse_expiry_time(
            response.get("expiry_time")
        )

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "status": response.get("transaction_status"),
            "transaction_id": response.get("transaction_id"),
            "qr_string": response.get("qr_string"),
            "qr_image_url": qr_image_url,
            "expiry_time": expiry_time,
            "raw_response": response,
        }
