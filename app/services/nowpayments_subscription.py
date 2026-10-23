"""
NOWPayments subscription payment orchestration.

This service is separate from PaymentService to preserve existing Midtrans
QRIS behavior. Customers select a pay-in asset and an explicitly configured
payout currency/address.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.subscription import SubscriptionPlan
from app.db.models.nowpayments_payment import NOWPaymentsPayment
from app.db.repositories.nowpayments_payment_repo import NOWPaymentsPaymentRepository
from app.services.nowpayments import (
    NOWPaymentsService,
    SUPPORTED_OUTCOME_CURRENCIES,
)


# Subscription price in USD. The actual coin amount is quoted by NOWPayments.
NOWPAYMENTS_PLAN_PRICES_USD: dict[SubscriptionPlan, Decimal] = {
    SubscriptionPlan.WEEKLY: Decimal("6.00"),
    SubscriptionPlan.MONTHLY: Decimal("24.00"),
    SubscriptionPlan.QUARTERLY: Decimal("72.00"),
}

# Auto-Capture add-on price in USD — 50% of the subscription price,
# mirroring the IDR add-on pricing rule. Capture orders paid through
# NOWPayments use NPC- order ids (NP family + C for Capture) so the IPN
# handler can activate the add-on instead of a base subscription.
NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD: dict[SubscriptionPlan, Decimal] = {
    SubscriptionPlan.WEEKLY: Decimal("3.00"),
    SubscriptionPlan.MONTHLY: Decimal("12.00"),
    SubscriptionPlan.QUARTERLY: Decimal("36.00"),
}


def generate_nowpayments_order_id(product: str = "subscription") -> str:
    """Generate a NOWPayments order id for the given product.

    Subscriptions keep the established NP-{uuid4 hex} format; capture
    add-on orders use the parallel NPC-{uuid4 hex} identity.
    """
    prefix = "NPC-" if product == "capture" else "NP-"
    return f"{prefix}{uuid4().hex}"


class NOWPaymentsSubscriptionService:
    """Create and persist NOWPayments subscription payment attempts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.provider = NOWPaymentsService()
        self.repository = NOWPaymentsPaymentRepository(session)

    async def get_available_outcome_currencies(self) -> list[dict[str, str]]:
        """Return supported destinations present in NOWPayments' live currency list."""
        return await self.provider.get_available_outcome_currencies()

    async def get_available_payment_currencies(
        self, *, plan: SubscriptionPlan, outcome_currency: str
    ) -> list[dict[str, object]]:
        """Return source assets whose live minimum fits this plan and destination."""
        price_usd = NOWPAYMENTS_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported subscription plan.")
        return await self.provider.get_available_payment_currencies(
            price_usd=price_usd, outcome_currency=outcome_currency
        )

    async def get_available_capture_payment_currencies(
        self, *, plan: SubscriptionPlan, outcome_currency: str
    ) -> list[dict[str, object]]:
        """Capture add-on variant of get_available_payment_currencies."""
        price_usd = NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported capture add-on plan.")
        return await self.provider.get_available_payment_currencies(
            price_usd=price_usd, outcome_currency=outcome_currency
        )

    async def create_capture_payment(
        self, *, user_id: int, plan: SubscriptionPlan, pay_currency: str,
        outcome_currency: str
    ) -> tuple[NOWPaymentsPayment, dict[str, object]]:
        """Create and persist a NOWPayments payment for the capture add-on.

        Mirrors create_payment, with three product differences: capture
        USD prices, an NPC- order id, and the stored plan prefixed with
        "capture_" so subscription-side lookups by (user, plan) can
        never pick up an add-on row by accident.
        """
        price_usd = NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported capture add-on plan.")
        if outcome_currency not in SUPPORTED_OUTCOME_CURRENCIES:
            raise ValueError("Unsupported NOWPayments destination currency.")
        minimum = await self.provider.get_minimum_payment_amount(
            pay_currency=pay_currency, outcome_currency=outcome_currency
        )
        if minimum["fiat_equivalent_usd"] > Decimal("2.00"):
            raise ValueError("The minimum for this currency pair is currently above $2.")
        if price_usd < minimum["fiat_equivalent_usd"]:
            raise ValueError("The selected capture add-on price is below the current minimum.")
        order_id = generate_nowpayments_order_id("capture")
        provider_payment = await self.provider.create_payment(
            price_usd=price_usd,
            pay_currency=pay_currency,
            outcome_currency=outcome_currency,
            order_id=order_id,
            description=f"{plan.value.title()} Auto-Capture add-on",
        )
        try:
            pay_amount = Decimal(str(provider_payment["pay_amount"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("NOWPayments returned an invalid crypto amount.") from exc
        payment = await self.repository.create(
            user_id=user_id,
            order_id=order_id,
            plan=f"capture_{plan.value}",
            price_amount=price_usd,
            pay_currency=str(provider_payment["pay_currency"]).lower(),
            outcome_currency=str(provider_payment.get("payout_currency") or outcome_currency).lower(),
            provider_payment_id=str(provider_payment["payment_id"]),
            invoice_id=str(provider_payment["invoice_id"]) if provider_payment.get("invoice_id") else None,
            pay_address=str(provider_payment["pay_address"]),
            pay_amount=pay_amount,
            payment_status=str(provider_payment.get("payment_status") or "waiting"),
        )
        return payment, provider_payment

    async def create_payment(
        self, *, user_id: int, plan: SubscriptionPlan, pay_currency: str,
        outcome_currency: str
    ) -> tuple[NOWPaymentsPayment, dict[str, object]]:
        """Create and persist a payment for the selected source/destination pair."""
        price_usd = NOWPAYMENTS_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported subscription plan.")
        if outcome_currency not in SUPPORTED_OUTCOME_CURRENCIES:
            raise ValueError("Unsupported NOWPayments destination currency.")
        minimum = await self.provider.get_minimum_payment_amount(
            pay_currency=pay_currency, outcome_currency=outcome_currency
        )
        if minimum["fiat_equivalent_usd"] > Decimal("2.00"):
            raise ValueError("The minimum for this currency pair is currently above $2.")
        if price_usd < minimum["fiat_equivalent_usd"]:
            raise ValueError("The selected subscription price is below the current minimum.")
        order_id = generate_nowpayments_order_id("subscription")
        provider_payment = await self.provider.create_payment(
            price_usd=price_usd,
            pay_currency=pay_currency,
            outcome_currency=outcome_currency,
            order_id=order_id,
            description=f"{plan.value.title()} subscription",
        )
        try:
            pay_amount = Decimal(str(provider_payment["pay_amount"]))
        except (KeyError, TypeError, ValueError, ArithmeticError) as exc:
            raise ValueError("NOWPayments returned an invalid crypto amount.") from exc
        payment = await self.repository.create(
            user_id=user_id,
            order_id=order_id,
            plan=plan.value,
            price_amount=price_usd,
            pay_currency=str(provider_payment["pay_currency"]).lower(),
            outcome_currency=str(provider_payment.get("payout_currency") or outcome_currency).lower(),
            provider_payment_id=str(provider_payment["payment_id"]),
            invoice_id=str(provider_payment["invoice_id"]) if provider_payment.get("invoice_id") else None,
            pay_address=str(provider_payment["pay_address"]),
            pay_amount=pay_amount,
            payment_status=str(provider_payment.get("payment_status") or "waiting"),
        )
        return payment, provider_payment