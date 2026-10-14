"""Read-only diagnostic for NOWPayments minimum payment amounts.

Run from the repository root:
    python scripts/check_nowpayments_minimums.py

Uses the selected NOWPAYMENTS_ENVIRONMENT. It never prints credentials and
does not create invoices or payments.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import get_settings


# Candidates commonly used for lower-cost payments; availability and minimums
# must be checked against the merchant's live API response.
CANDIDATE_CURRENCIES = (
    "usdttrc20",
    "usdcbase",
    "trx",
    "ltc",
    "xrp",
    "xlm",
    "bch",
    "bnbbsc",
    "sol",
    "matic",
)


async def main() -> None:
    settings = get_settings()
    api_key = settings.nowpayments_active_api_key.get_secret_value().strip()
    base_url = settings.nowpayments_active_base_url.rstrip("/")

    if not api_key:
        raise SystemExit(
            "NOWPayments API key is empty for the selected environment. "
            "Set the correct key in .env; do not paste it into chat."
        )

    print(f"Environment: {settings.nowpayments_environment}")
    print(f"API host: {base_url}")
    print("Read-only checks only; no payment or invoice will be created.\n")

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"x-api-key": api_key},
        timeout=httpx.Timeout(20.0),
    ) as client:
        try:
            currencies_response = await client.get("/currencies")
            currencies_response.raise_for_status()
            raw_currencies = currencies_response.json().get("currencies", [])
            available = {str(code).lower() for code in raw_currencies}
            print("Currencies endpoint responded successfully.")
        except (httpx.HTTPError, ValueError) as exc:
            available = set()
            print(f"Could not retrieve currency list: {type(exc).__name__}")

        print("\nUSD -> candidate currency minimums:")
        for currency in CANDIDATE_CURRENCIES:
            if available and currency not in available:
                print(f"{currency}: not listed by /currencies for this account")
                continue
            try:
                response = await client.get(
                    "/min-amount",
                    params={
                        "currency_from": "usd",
                        "currency_to": currency,
                        "fiat_equivalent": "usd",
                    },
                )
                if response.is_error:
                    print(f"{currency}: HTTP {response.status_code}")
                    continue
                data = response.json()
                minimum = data.get("min_amount")
                equivalent = data.get("fiat_equivalent")
                print(
                    f"{currency}: min_amount={minimum}, "
                    f"fiat_equivalent_usd={equivalent}"
                )
                if equivalent is not None:
                    try:
                        if Decimal(str(equivalent)) <= Decimal("2"):
                            print("  -> API reports a minimum equivalent at or below $2")
                    except (InvalidOperation, TypeError, ValueError):
                        pass
            except (httpx.HTTPError, ValueError) as exc:
                print(f"{currency}: request/response error: {type(exc).__name__}")

    print(
        "\nImportant: this checks the API-reported minimum for USD -> asset. "
        "Hosted invoice checkout can apply additional account, currency, and "
        "network constraints. Confirm the exact checkout error before changing "
        "subscription prices. A payout wallet being configured does not lower "
        "the customer's payment minimum."
    )


if __name__ == "__main__":
    asyncio.run(main())
