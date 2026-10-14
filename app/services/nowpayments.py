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
from urllib.parse import urlparse

import httpx

from app.core.config import get_settings


# Exclude XRP/XLM because those networks can require a destination tag/memo.
# The live menu is filtered against the merchant USDC Base outcome wallet.
# Source assets customers may send (currency_from).
SUPPORTED_PAYMENT_CURRENCIES: dict[str, str] = {
    "ethbase": "ETH (Base)",
    "usdcbase": "USDC (Base)",
    "trx": "TRX (TRON)",
    "bnbbsc": "BNB (BSC)",
    "sol": "Solana (SOL)",
    "eth": "ETH (Mainnet)",
    "usdttrc20": "USDT-TRX (TRON)",
    "usdtbsc": "USDT-BSC (BEP20)",
    "usdtsol": "USDT-Solana",
    "usdt": "USDT-ETH (ERC20)",
    "ltc": "Litecoin (LTC)",
    "bch": "Bitcoin Cash (BCH)",
    "dash": "Dash (DASH)",
    "tusd": "TrueUSD (TUSD)",
    "matic": "Polygon (MATIC)",
}

# Destinations the merchant explicitly wants to support (currency_to).
SUPPORTED_OUTCOME_CURRENCIES: dict[str, str] = {
    "ethbase": "ETH Base",
    "usdcbase": "USDC Base",
    "trx": "TRX (TRON)",
    "bnbbsc": "BNB (BSC)",
    "sol": "Solana (SOL)",
    "eth": "ETH (Mainnet)",
    "usdttrc20": "USDT-TRX (TRON)",
    "usdtbsc": "USDT-BSC (BEP20)",
    "usdtsol": "USDT-Solana",
    "usdt": "USDT-ETH (ERC20)",
}

OUTCOME_CURRENCY = "usdcbase"


class NOWPaymentsError(RuntimeError):
    """Raised when a NOWPayments API request fails or returns invalid data."""


class NOWPaymentsService:
    """Small async client for the NOWPayments Payments API."""

    def __init__(self) -> None:
        settings = get_settings()
        self._environment = settings.nowpayments_environment
        self._api_key = settings.nowpayments_active_api_key.get_secret_value().strip()
        self._base_url = settings.nowpayments_active_base_url.rstrip("/")
        self._ipn_callback_url = settings.nowpayments_ipn_callback_url.strip()
        self._payout_addresses: dict[str, str] = {}
        for item in settings.nowpayments_payout_addresses.split(","):
            if "=" not in item:
                continue
            currency, address = item.split("=", 1)
            currency, address = currency.strip().lower(), address.strip()
            if currency and address and not address.startswith("YOUR_"):
                self._payout_addresses[currency] = address

        expected_host = (
            "api-sandbox.nowpayments.io"
            if self._environment == "sandbox"
            else "api.nowpayments.io"
        )
        actual_host = urlparse(self._base_url).hostname
        if actual_host != expected_host:
            raise NOWPaymentsError(
                f"NOWPayments {self._environment} mode requires API host {expected_host}."
            )

    def _headers(self) -> dict[str, str]:
        if not self._api_key:
            raise NOWPaymentsError(
                "NOWPayments API key is not configured for the selected environment."
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

    async def get_minimum_payment_amount(
        self,
        *,
        pay_currency: str,
        outcome_currency: str = OUTCOME_CURRENCY,
    ) -> dict[str, Any]:
        """Return live minimum for a currency_from -> currency_to pair."""
        source = pay_currency.strip().lower()
        target = outcome_currency.strip().lower()
        if source not in SUPPORTED_PAYMENT_CURRENCIES:
            raise ValueError("Unsupported NOWPayments source currency.")
        if target not in SUPPORTED_OUTCOME_CURRENCIES:
            raise ValueError("Unsupported NOWPayments destination currency.")
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    f"{self._base_url}/min-amount",
                    headers=self._headers(),
                    params={
                        "currency_from": source,
                        "currency_to": target,
                        "fiat_equivalent": "usd",
                    },
                )
        except httpx.HTTPError as exc:
            raise NOWPaymentsError("Could not check the current NOWPayments minimum. Please retry.") from exc
        if response.is_error:
            raise NOWPaymentsError(f"NOWPayments minimum check returned HTTP {response.status_code}.")
        try:
            result = response.json()
            minimum_amount = Decimal(str(result["min_amount"]))
            fiat_equivalent = Decimal(str(result["fiat_equivalent"]))
        except (ValueError, TypeError, KeyError, ArithmeticError) as exc:
            raise NOWPaymentsError("NOWPayments returned an invalid minimum-amount response.") from exc
        if not minimum_amount.is_finite() or minimum_amount <= 0 or not fiat_equivalent.is_finite() or fiat_equivalent <= 0:
            raise NOWPaymentsError("NOWPayments returned invalid minimum-amount values.")
        return {
            "currency_from": source,
            "currency_to": target,
            "min_amount": minimum_amount,
            "fiat_equivalent_usd": fiat_equivalent,
        }

    async def get_available_outcome_currencies(self) -> list[dict[str, str]]:
        """Only show configured destination codes currently listed by NOWPayments."""
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.get(
                    f"{self._base_url}/currencies",
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise NOWPaymentsError("Could not retrieve NOWPayments currencies.") from exc
        if response.is_error:
            raise NOWPaymentsError(f"NOWPayments currency list returned HTTP {response.status_code}.")
        try:
            result = response.json()
        except ValueError as exc:
            raise NOWPaymentsError("NOWPayments returned an invalid currency list.") from exc
        raw_codes = result.get("currencies", []) if isinstance(result, dict) else []
        available_codes = {str(code).lower() for code in raw_codes}
        return [
            {"code": code, "label": label}
            for code, label in SUPPORTED_OUTCOME_CURRENCIES.items()
            if code in available_codes and code in self._payout_addresses
        ]

    async def get_available_payment_currencies(
        self,
        *,
        price_usd: Decimal,
        outcome_currency: str = OUTCOME_CURRENCY,
        max_minimum_usd: Decimal = Decimal("2.00"),
    ) -> list[dict[str, Any]]:
        """List source assets whose live minimum for the selected destination is <= $2."""
        if price_usd <= 0:
            raise ValueError("price_usd must be greater than zero.")
        if outcome_currency not in SUPPORTED_OUTCOME_CURRENCIES:
            raise ValueError("Unsupported NOWPayments destination currency.")
        available: list[dict[str, Any]] = []
        for code, label in SUPPORTED_PAYMENT_CURRENCIES.items():
            try:
                minimum = await self.get_minimum_payment_amount(
                    pay_currency=code,
                    outcome_currency=outcome_currency,
                )
            except NOWPaymentsError:
                continue
            minimum_usd = minimum["fiat_equivalent_usd"]
            if minimum_usd <= max_minimum_usd and price_usd >= minimum_usd:
                available.append({
                    "code": code,
                    "label": label,
                    "minimum_amount": minimum["min_amount"],
                    "minimum_usd": minimum_usd,
                    "recommended": code == outcome_currency,
                })
        return available

    async def create_invoice(
        self,
        *,
        price_usd: Decimal,
        order_id: str,
        description: str,
        success_url: str | None = None,
        cancel_url: str | None = None,
    ) -> dict[str, Any]:
        """Create a hosted invoice; the customer chooses a supported asset at checkout.

        The invoice API uses a USD-denominated reference price. The current
        package menu is intended to approximate the agreed USDT prices, so the
        checkout amount must be reviewed before production use.
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
        self, *, price_usd: Decimal, pay_currency: str,
        outcome_currency: str = OUTCOME_CURRENCY, order_id: str, description: str,
    ) -> dict[str, Any]:
        """Create a direct payment for a selected source and payout currency."""
        if price_usd <= 0:
            raise ValueError("price_usd must be greater than zero.")
        source = pay_currency.strip().lower()
        target = outcome_currency.strip().lower()
        if source not in SUPPORTED_PAYMENT_CURRENCIES:
            raise ValueError("Unsupported NOWPayments source currency.")
        if target not in SUPPORTED_OUTCOME_CURRENCIES:
            raise ValueError("Unsupported NOWPayments destination currency.")
        payout_address = self._payout_addresses.get(target)
        if not payout_address:
            raise NOWPaymentsError("The selected payout currency has no configured wallet address.")
        if not self._ipn_callback_url.startswith("https://"):
            raise NOWPaymentsError("NOWPayments IPN callback must be a public HTTPS URL.")
        result = await self._post(
            "/payment",
            {
                "price_amount": float(price_usd),
                "price_currency": "usd",
                "pay_currency": source,
                "payout_address": payout_address,
                "payout_currency": target,
                "ipn_callback_url": self._ipn_callback_url,
                "order_id": order_id,
                "order_description": description[:255],
            },
        )
        required = ("payment_id", "pay_address", "pay_amount", "pay_currency")
        if any(result.get(field) in (None, "") for field in required):
            raise NOWPaymentsError("NOWPayments payment response is missing required payment details.")
        result.setdefault("payout_currency", target)
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