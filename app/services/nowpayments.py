"""
NOWPayments API client.

This module only handles provider HTTP calls and IPN signature verification.
Database persistence, webhook routing, and subscription activation are kept
in separate layers so the existing Midtrans flow remains untouched.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from decimal import Decimal
from typing import Any

import httpx

from app.core.config import get_settings


SUPPORTED_PAYMENT_CURRENCIES: dict[str, str] = {
    "usdcbase": "USDC on Base",
    "ethbase": "ETH on Base",
    "trx": "TRX on TRON",
}


class NOWPaymentsError(RuntimeError):
    """Raised when a NOWPayments API request fails or returns invalid data."""


class NOWPaymentsService:
    """Small async client for the NOWPayments Payments API."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.nowpayments_api_key.get_secret_value().strip()
        self._base_url = settings.nowpayments_base_url.rstrip("/")
        self._ipn_callback_url = settings.nowpayments_ipn_callback_url.strip()

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise NOWPaymentsError(
                "NOWPayments API key is not configured. Set NOWPAYMENTS_API_KEY."
            )
        return {
            "x-api-key": self._api_key,
            "Content-Type": "application/json",
        }

    async def _post(
        self,
        path: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        url = f"{self._base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    url,
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as exc:
            raise NOWPaymentsError(
                "Could not connect to NOWPayments. Check network connectivity."
            ) from exc

        if response.is_error:
            # Avoid returning raw provider response text, which may contain
            # implementation details. The status code is enough for logs/UI.
            raise NOWPaymentsError(
                f"NOWPayments API returned HTTP {response.status_code}."
            )

        try:
            result = response.json()
        except ValueError as exc:
            raise NOWPaymentsError(
                "NOWPayments returned an invalid JSON response."
            ) from exc

        if not isinstance(result, dict):
            raise NOWPaymentsError("NOWPayments returned an unexpected response.")
        return result

    async def create_invoice(
        self,
        *,
        price_usd: Decimal,
        order_id: str,
        description: str,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a hosted invoice and return the provider response.

        NOWPayments' invoice API documents price_amount in a supported fiat
        price currency. For the agreed USDT-denominated menu, this uses the
        near-1:1 USD equivalent; the exact invoice semantics must be shown to
        the customer before the flow is enabled in production.
        """
        if price_usd <= 0:
            raise ValueError("price_usd must be greater than zero.")
        if not self._ipn_callback_url.startswith("https://"):
            raise NOWPaymentsError(
                "NOWPayments IPN callback must be a public HTTPS URL."
            )

        payload: dict[str, Any] = {
            "price_amount": float(price_usd),
            "price_currency": "usd",
            "order_id": order_id,
            "order_description": description[:255],
            "ipn_callback_url": self._ipn_callback_url,
        }
        if success_url:
            payload["success_url"] = success_url
        if cancel_url:
            payload["cancel_url"] = cancel_url

        result = await self._post("/invoice", payload)
        if not result.get("id") or not result.get("invoice_url"):
            raise NOWPaymentsError(
                "NOWPayments invoice response is missing id or invoice_url."
            )
        return result

    async def create_payment(
        self,
        *,
        price_usd: Decimal,
        pay_currency: str,
        order_id: str,
        description: str,
    ) -> dict[str, Any]:
        """Create a payment for one explicitly selected asset/network."""
        if price_usd <= 0:
            raise ValueError("price_usd must be greater than zero.")
        normalized_currency = pay_currency.strip().lower()
        if normalized_currency not in SUPPORTED_PAYMENT_CURRENCIES:
            raise ValueError("Unsupported NOWPayments payment currency.")
        if not self._ipn_callback_url.startswith("https://"):
            raise NOWPaymentsError(
                "NOWPayments IPN callback must be a public HTTPS URL."
            )

        result = await self._post(
            "/payment",
            {
                "price_amount": float(price_usd),
                "price_currency": "usd",
                "pay_currency": normalized_currency,
                "ipn_callback_url": self._ipn_callback_url,
                "order_id": order_id,
                "order_description": description[:255],
            },
        )
        required = ("payment_id", "pay_address", "pay_amount", "pay_currency")
        if any(result.get(field) in (None, "") for field in required):
            raise NOWPaymentsError(
                "NOWPayments payment response is missing required payment details."
            )
        return result

    @staticmethod
    def verify_ipn_signature(
        payload: dict[str, Any],
        received_signature: str | None,
        ipn_secret: str,
    ) -> bool:
        """Verify x-nowpayments-sig using the documented sorted-JSON HMAC."""
        if not received_signature or not ipn_secret:
            return False

        canonical_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        expected_signature = hmac.new(
            ipn_secret.strip().encode("utf-8"),
            canonical_payload.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest()
        return hmac.compare_digest(expected_signature, received_signature.strip())
