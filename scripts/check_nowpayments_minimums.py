"""Read-only diagnostic for NOWPayments minimum payment pairs.

Run from the repository root:
    python scripts/check_nowpayments_minimums.py

Uses the selected NOWPAYMENTS_ENVIRONMENT. It never prints credentials and
does not create invoices or payments.

Important:
- USD -> asset checks are not equivalent to crypto pay-in -> merchant outcome
  wallet checks for a hosted invoice.
- The merchant's configured outcome wallet(s) determine which "To" currency
  is relevant. Pass NOWPAYMENTS_OUTCOME_CURRENCIES as a comma-separated list
  of exact NOWPayments currency codes to test those pairs.
- Without that setting, the script tests a candidate pair matrix for diagnosis;
  those results are exploratory and must not be treated as proof that checkout
  will accept that minimum for the merchant's account.
"""
from __future__ import annotations

import asyncio
import os
from decimal import Decimal, InvalidOperation

import httpx

from app.core.config import get_settings


# Assets that NOWPayments has listed as potential low-minimum pay-in options.
CANDIDATE_CURRENCIES = (
    "trx",
    "xrp",
    "xlm",
    "ltc",
    "bch",
    "bnbmainnet",
    "dash",
    "xmr",
    "zec",
    "eos",
    "qtum",
    "tusd",
    "usdttrc20",
    "usdcbase",
    "ethbase",
    "sol",
    "matic",
)


def _parse_outcome_currencies() -> tuple[str, ...]:
    raw = os.getenv("NOWPAYMENTS_OUTCOME_CURRENCIES", "").strip()
    if not raw:
        return ()
    return tuple(dict.fromkeys(
        code.strip().lower() for code in raw.split(",") if code.strip()
    ))


async def main() -> None:
    settings = get_settings()
    api_key = settings.nowpayments_active_api_key.get_secret_value().strip()
    base_url = settings.nowpayments_active_base_url.rstrip("/")
    outcome_currencies = _parse_outcome_currencies()

    if not api_key:
        raise SystemExit(
            "NOWPayments API key is empty for the selected environment. "
            "Set the correct key in .env; do not paste it into chat."
        )

    print(f"Environment: {settings.nowpayments_environment}")
    print(f"API host: {base_url}")
    print("Read-only checks only; no payment or invoice will be created.")

    if outcome_currencies:
        targets = outcome_currencies
        print("To currencies: configured by NOWPAYMENTS_OUTCOME_CURRENCIES")
    else:
        targets = CANDIDATE_CURRENCIES
        print(
            "WARNING: NOWPAYMENTS_OUTCOME_CURRENCIES is not set. "
            "Testing exploratory candidate pairs, not confirming merchant checkout."
        )

    async with httpx.AsyncClient(
        base_url=base_url,
        headers={"x-api-key": api_key},
        timeout=httpx.Timeout(20.0),
    ) as client:
        try:
            response = await client.get("/currencies")
            response.raise_for_status()
            raw = response.json().get("currencies", [])
            available = {str(code).lower() for code in raw}
            print(f"Currency list retrieved ({len(available)} listed codes).")
        except (httpx.HTTPError, ValueError) as exc:
            available = set()
            print(f"Could not retrieve currency list: {type(exc).__name__}")

        print("\n1) USD -> outcome currency baseline:")
        for target in targets:
            if available and target not in available:
                print(f"usd -> {target}: target not listed by /currencies")
                continue
            await _check_pair(client, "usd", target)

        print("\n2) Candidate crypto pay-in -> outcome currency pairs:")
        candidates = tuple(
            code for code in CANDIDATE_CURRENCIES
            if not available or code in available
        )
        found = []
        for target in targets:
            if available and target not in available:
                continue
            for source in candidates:
                if source == target:
                    # Same-asset transfers are useful and commonly have low fees.
                    pass
                result = await _check_pair(client, source, target, quiet=True)
                if result is not None and result <= Decimal("2"):
                    found.append((source, target, result))
                    print(f"  <= $2 candidate: {source} -> {target} (USD equivalent {result})")

        if not found:
            print("No tested pair returned a fiat equivalent at or below $2.")
            print(
                "This is not proof that no pair works: availability and minimums "
                "can change, and the configured outcome currencies must be correct."
            )

    print(
        "\nNext step: confirm the exact outcome-wallet currency codes shown in "
        "NOWPayments Dashboard. Set them in the local shell before rerunning, e.g. "
        "PowerShell: $env:NOWPAYMENTS_OUTCOME_CURRENCIES='usdttrc20,usdcbase,ethbase'. "
        "Use only codes that match wallets actually configured in your account. "
        "Do not share API keys or IPN secrets."
    )


async def _check_pair(
    client: httpx.AsyncClient,
    source: str,
    target: str,
    *,
    quiet: bool = False,
) -> Decimal | None:
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
            if not quiet:
                print(f"{source} -> {target}: HTTP {response.status_code}")
            return None
        data = response.json()
        equivalent = data.get("fiat_equivalent")
        minimum = data.get("min_amount")
        if not quiet:
            print(
                f"{source} -> {target}: min_amount={minimum}, "
                f"fiat_equivalent_usd={equivalent}"
            )
        if equivalent is None:
            return None
        try:
            return Decimal(str(equivalent))
        except (InvalidOperation, TypeError, ValueError):
            return None
    except (httpx.HTTPError, ValueError) as exc:
        if not quiet:
            print(f"{source} -> {target}: request/response error: {type(exc).__name__}")
        return None


if __name__ == "__main__":
    asyncio.run(main())
