"""Read-only NOWPayments minimum-amount diagnostic.

Run from the repository root:
    python scripts/check_nowpayments_minimums.py

Uses the currently selected NOWPAYMENTS_ENVIRONMENT and never prints API keys.
It checks source-asset -> configured outcome-wallet currency pairs; NOWPayments
minimums are dynamic and depend on the exact pair/network/fee settings.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import get_settings


# Common low-minimum assets listed by NOWPayments documentation, plus assets
# shown in the operator's payout-wallet configuration.
SOURCE_CURRENCIES = (
    "trx",
    "ltc",
    "xrp",
    "xlm",
    "bch",
    "bnbbsc",
    "usdttrc20",
    "usdcbase",
    "ethbase",
)
OUTCOME_CURRENCIES = (
    "ethbase",
    "usdttrc20",
    "usdcbase",
    "sol",
    "btc",
    "trx",
    "bnbbsc",
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
    print("Read-only checks only; no payment/invoice will be created.")
    print("Minimums can change with network fees and merchant wallet settings.\n")

    headers = {"x-api-key": api_key}
    timeout = httpx.Timeout(20.0)
    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=timeout,
    ) as client:
        for target in OUTCOME_CURRENCIES:
            # Official endpoint supports fiat currency_from and returns the
            # minimum for the specified outcome currency, useful as a first
            # diagnostic of the destination-wallet configuration.
            try:
                response = await client.get(
                    "/min-amount",
                    params={
                        "currency_from": "usd",
                        "currency_to": target,
                        "fiat_equivalent": "usd",
                    },
                )
                if response.is_error:
                    print(f"USD -> {target}: HTTP {response.status_code}")
                    continue
                data = response.json()
                minimum = data.get("min_amount")
                fiat = data.get("fiat_equivalent")
                print(f"USD -> {target}: min_amount={minimum}, fiat_equivalent={fiat}")
            except (httpx.HTTPError, ValueError) as exc:
                print(f"USD -> {target}: request/response error: {type(exc).__name__}")

        print("\nCustomer source-asset -> payout-wallet pair checks:")
        for source in SOURCE_CURRENCIES:
            for target in OUTCOME_CURRENCIES:
                try:
                    response = await client.get(
                        "/min-amount",
                        params={
                            "currency_from": source,
                            "currency_to": target,
                            "fiat_equivalent": "usd",
                        },
                    )
                    if response.is_error:
                        # Unsupported pairs are expected; keep output compact.
                        continue
                    data = response.json()
                    minimum = data.get("min_amount")
                    fiat = data.get("fiat_equivalent")
                    if fiat is not None:
                        try:
                            usd_value = Decimal(str(fiat))
                        except (InvalidOperation, TypeError, ValueError):
                            usd_value = None
                        if usd_value is not None and usd_value <= Decimal("5"):
                            print(
                                f"{source} -> {target}: min_amount={minimum}, "
                                f"fiat_equivalent_usd={fiat}"
                            )
                except (httpx.HTTPError, ValueError):
                    continue

    print(
        "\nInterpretation: these are API-reported minimums for tested pairs, "
        "not a guarantee that every hosted checkout choice will accept $2. "
        "Compare them with the exact checkout error and NOWPayments Status Page."
    )


if __name__ == "__main__":
    asyncio.run(main())
