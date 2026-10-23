"""
app.services.midtrans
~~~~~~~~~~~~~~~~~~~~~
Midtrans payment gateway integration.
"""

from datetime import datetime, timezone, timedelta
from uuid import uuid4

import httpx
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
            SubscriptionPlan.QUARTERLY: settings.subscription_3_monthly_price,
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

    # ------------------------------------------------------------------
    # Auto-Capture add-on (/capture) — a separate product sold through
    # the same gateway. Capture orders carry the CAP- prefix so payment
    # success handling can activate the add-on instead of a subscription:
    #   CAP-{user_id}-{plan_code}-{uuid12}   plan_code: W / M / Q
    # ------------------------------------------------------------------
    _CAPTURE_PLAN_CODES = {
        SubscriptionPlan.WEEKLY: "W",
        SubscriptionPlan.MONTHLY: "M",
        SubscriptionPlan.QUARTERLY: "Q",
    }

    @staticmethod
    def _get_capture_price(plan: SubscriptionPlan) -> int:
        """Return the configured price for a capture add-on plan."""
        settings = get_settings()

        prices = {
            SubscriptionPlan.WEEKLY: settings.capture_weekly_price,
            SubscriptionPlan.MONTHLY: settings.capture_monthly_price,
            SubscriptionPlan.QUARTERLY: settings.capture_3_monthly_price,
        }

        try:
            return prices[plan]
        except KeyError as exc:
            raise MidtransError(
                f"Unsupported capture add-on plan: {plan}"
            ) from exc

    @staticmethod
    def generate_capture_order_id(user_id: int, plan: SubscriptionPlan) -> str:
        """Generate a unique Midtrans order ID for a capture add-on order."""
        try:
            plan_code = MidtransService._CAPTURE_PLAN_CODES[plan]
        except KeyError as exc:
            raise MidtransError(
                f"Unsupported capture add-on plan: {plan}"
            ) from exc
        return f"CAP-{user_id}-{plan_code}-{uuid4().hex[:12].upper()}"

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


    def create_capture_qris_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> dict:
        """Create a Midtrans QRIS transaction for a capture add-on purchase.

        Identical charge shape to create_qris_transaction (QRIS with the
        gopay acquirer and 15-minute expiry); only the product differs —
        capture price and a CAP- order id.
        """
        amount = self._get_capture_price(plan)
        order_id = self.generate_capture_order_id(user_id, plan)

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

    def create_payment_link_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> dict:
        """Create a fixed-amount hosted Midtrans Payment Link."""
        amount = self._get_price(plan)
        # Payment Link documents allow order_id up to 36 characters.
        # Use a compact ID so the quarterly plan also stays within that limit.
        plan_code = {
            SubscriptionPlan.WEEKLY: "W",
            SubscriptionPlan.MONTHLY: "M",
            SubscriptionPlan.QUARTERLY: "Q",
            SubscriptionPlan.LIFETIME: "L",
        }[plan]
        order_id = f"PL-{user_id}-{plan_code}-{uuid4().hex[:12].upper()}"
        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")
        if len(order_id) > 36:
            raise MidtransError("Generated Payment Link order_id exceeds Midtrans' 36-character limit.")

        settings = get_settings()
        base_url = (
            "https://api.midtrans.com"
            if settings.midtrans_is_production
            else "https://api.sandbox.midtrans.com"
        )
        payload = {
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "title": f"{plan.value.title()} Subscription"[:40],
            "item_details": [
                {
                    "id": f"subscription-{plan.value}",
                    "price": amount,
                    "quantity": 1,
                    "name": f"{plan.value.title()} Subscription",
                }
            ],
            "usage_limit": 1,
            "expiry": {
                "duration": 30,
                "unit": "minutes",
            },
        }

        try:
            response = httpx.post(
                f"{base_url}/v1/payment-links",
                json=payload,
                auth=(settings.midtrans_server_key.get_secret_value(), ""),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=20.0,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    error_body = exc.response.json()
                    detail_value = (
                        error_body.get("error_messages")
                        or error_body.get("message")
                        or error_body.get("error")
                        or error_body.get("status_message")
                    )
                    detail = str(detail_value or error_body)[:300]
                except (ValueError, AttributeError):
                    detail = exc.response.text[:300]
            raise MidtransError(
                f"Failed to create Midtrans Payment Link{': ' + detail if detail else ''}"
            ) from exc

        payment_url = result.get("payment_url")
        if not payment_url or not str(payment_url).startswith("https://"):
            raise MidtransError("Midtrans did not return a valid HTTPS payment_url.")

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "status": "pending",
            "transaction_id": None,
            "payment_url": payment_url,
            "expiry_time": None,
            "raw_response": result,
        }

    def create_capture_payment_link_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> dict:
        """Create a hosted Midtrans Payment Link for a capture add-on order.

        Uses the CAP- order id, which (like PL-) stays within the
        36-character Payment Link limit.
        """
        amount = self._get_capture_price(plan)
        order_id = self.generate_capture_order_id(user_id, plan)
        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")
        if len(order_id) > 36:
            raise MidtransError("Generated Payment Link order_id exceeds Midtrans' 36-character limit.")

        settings = get_settings()
        base_url = (
            "https://api.midtrans.com"
            if settings.midtrans_is_production
            else "https://api.sandbox.midtrans.com"
        )
        payload = {
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "title": f"{plan.value.title()} Auto-Capture Add-on"[:40],
            "item_details": [
                {
                    "id": f"capture-addon-{plan.value}",
                    "price": amount,
                    "quantity": 1,
                    "name": f"{plan.value.title()} Auto-Capture Add-on",
                }
            ],
            "usage_limit": 1,
            "expiry": {
                "duration": 30,
                "unit": "minutes",
            },
        }

        try:
            response = httpx.post(
                f"{base_url}/v1/payment-links",
                json=payload,
                auth=(settings.midtrans_server_key.get_secret_value(), ""),
                headers={"Accept": "application/json", "Content-Type": "application/json"},
                timeout=20.0,
            )
            response.raise_for_status()
            result = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            detail = ""
            if isinstance(exc, httpx.HTTPStatusError):
                try:
                    error_body = exc.response.json()
                    detail_value = (
                        error_body.get("error_messages")
                        or error_body.get("message")
                        or error_body.get("error")
                        or error_body.get("status_message")
                    )
                    detail = str(detail_value or error_body)[:300]
                except (ValueError, AttributeError):
                    detail = exc.response.text[:300]
            raise MidtransError(
                f"Failed to create Midtrans Payment Link{': ' + detail if detail else ''}"
            ) from exc

        payment_url = result.get("payment_url")
        if not payment_url or not str(payment_url).startswith("https://"):
            raise MidtransError("Midtrans did not return a valid HTTPS payment_url.")

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "status": "pending",
            "transaction_id": None,
            "payment_url": payment_url,
            "expiry_time": None,
            "raw_response": result,
        }

    def create_capture_mandiri_bill_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> dict:
        """Create a Mandiri Bill Payment (E-Channel) for a capture add-on."""
        amount = self._get_capture_price(plan)
        order_id = self.generate_capture_order_id(user_id, plan)
        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")

        parameter = {
            "payment_type": "echannel",
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "echannel": {
                "bill_info1": "Auto-Capture Add-on",
                "bill_info2": f"{plan.value.title()} plan",
            },
        }

        try:
            response = self._client.charge(parameter)
        except Exception as exc:
            raise MidtransError(
                f"Failed to create Midtrans Mandiri Bill transaction: {exc}"
            ) from exc

        biller_code = response.get("biller_code")
        bill_key = response.get("bill_key")
        if not biller_code or not bill_key:
            raise MidtransError(
                "Midtrans did not return the Mandiri biller_code and bill_key."
            )

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "payment_type": "echannel",
            "biller_code": str(biller_code),
            "bill_key": str(bill_key),
            "status": response.get("transaction_status"),
            "transaction_id": response.get("transaction_id"),
            "expiry_time": self._parse_expiry_time(response.get("expiry_time")),
            "raw_response": response,
        }

    def create_capture_bank_transfer_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
        bank: str,
    ) -> dict:
        """Create a Virtual Account transaction (BRI/BNI/CIMB) for a capture add-on."""
        supported_banks = {"bri", "bni", "cimb"}
        normalized_bank = bank.strip().lower()
        if normalized_bank not in supported_banks:
            raise MidtransError(
                f"Unsupported Virtual Account bank: {bank}. "
                "Supported banks are BRI, BNI, and CIMB."
            )

        amount = self._get_capture_price(plan)
        order_id = self.generate_capture_order_id(user_id, plan)
        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")

        parameter = {
            "payment_type": "bank_transfer",
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "bank_transfer": {"bank": normalized_bank},
        }
        try:
            response = self._client.charge(parameter)
        except Exception as exc:
            raise MidtransError(
                f"Failed to create Midtrans {normalized_bank.upper()} Virtual Account: {exc}"
            ) from exc

        bank_transfer = response.get("bank_transfer") or {}
        va_number = bank_transfer.get("va_number") or response.get("va_number")
        if not va_number:
            va_numbers = response.get("va_numbers") or []
            for item in va_numbers:
                if str(item.get("bank", "")).lower() == normalized_bank:
                    va_number = item.get("va_number")
                    break
            if not va_number and len(va_numbers) == 1:
                va_number = va_numbers[0].get("va_number")

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "bank": normalized_bank,
            "va_number": str(va_number) if va_number else None,
            "status": response.get("transaction_status"),
            "transaction_id": response.get("transaction_id"),
            "expiry_time": self._parse_expiry_time(response.get("expiry_time")),
            "raw_response": response,
        }

    def create_mandiri_bill_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> dict:
        """Create a Midtrans Mandiri Bill Payment (E-Channel) transaction."""
        amount = self._get_price(plan)
        order_id = self.generate_order_id(user_id, plan)
        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")

        parameter = {
            "payment_type": "echannel",
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "echannel": {
                "bill_info1": "Subscription",
                "bill_info2": f"{plan.value.title()} plan",
            },
        }

        try:
            response = self._client.charge(parameter)
        except Exception as exc:
            raise MidtransError(
                f"Failed to create Midtrans Mandiri Bill transaction: {exc}"
            ) from exc

        biller_code = response.get("biller_code")
        bill_key = response.get("bill_key")
        if not biller_code or not bill_key:
            raise MidtransError(
                "Midtrans did not return the Mandiri biller_code and bill_key."
            )

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "payment_type": "echannel",
            "biller_code": str(biller_code),
            "bill_key": str(bill_key),
            "status": response.get("transaction_status"),
            "transaction_id": response.get("transaction_id"),
            "expiry_time": self._parse_expiry_time(response.get("expiry_time")),
            "raw_response": response,
        }


    def create_bank_transfer_transaction(
        self,
        user_id: int,
        plan: SubscriptionPlan,
        bank: str,
    ) -> dict:
        """Create a Midtrans Virtual Account transaction for BRI, BNI, or CIMB."""
        supported_banks = {"bri", "bni", "cimb"}
        normalized_bank = bank.strip().lower()
        if normalized_bank not in supported_banks:
            raise MidtransError(
                f"Unsupported Virtual Account bank: {bank}. "
                "Supported banks are BRI, BNI, and CIMB."
            )

        amount = self._get_price(plan)
        order_id = self.generate_order_id(user_id, plan)
        if amount <= 0:
            raise MidtransError("Payment amount must be greater than zero.")

        parameter = {
            "payment_type": "bank_transfer",
            "transaction_details": {
                "order_id": order_id,
                "gross_amount": amount,
            },
            "bank_transfer": {"bank": normalized_bank},
        }
        try:
            response = self._client.charge(parameter)
        except Exception as exc:
            raise MidtransError(
                f"Failed to create Midtrans {normalized_bank.upper()} Virtual Account: {exc}"
            ) from exc

        bank_transfer = response.get("bank_transfer") or {}
        va_number = bank_transfer.get("va_number") or response.get("va_number")
        if not va_number:
            va_numbers = response.get("va_numbers") or []
            for item in va_numbers:
                if str(item.get("bank", "")).lower() == normalized_bank:
                    va_number = item.get("va_number")
                    break
            if not va_number and len(va_numbers) == 1:
                va_number = va_numbers[0].get("va_number")

        return {
            "order_id": order_id,
            "plan": plan.value,
            "amount": amount,
            "currency": "IDR",
            "bank": normalized_bank,
            "va_number": str(va_number) if va_number else None,
            "status": response.get("transaction_status"),
            "transaction_id": response.get("transaction_id"),
            "expiry_time": self._parse_expiry_time(response.get("expiry_time")),
            "raw_response": response,
        }