"""
NOWPayments subscription payment orchestration.

This service is separate from PaymentService to preserve existing Midtrans
QRIS behavior. Customers select a pay-in asset; NOWPayments routes settlement
to the merchant's configured USDC Base outcome wallet.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.subscription import SubscriptionPlan
from app.db.models.nowpayments_payment import NOWPaymentsPayment
from app.db.repositories.nowpayments_payment_repo import NOWPaymentsPaymentRepository
from app.services.nowpayments import NOWPaymentsService


# Subscription price in USD. The actual coin amount is quoted by NOWPayments.
NOWPAYMENTS_PLAN_PRICES_USD: dict[SubscriptionPlan, Decimal] = {
    SubscriptionPlan.WEEKLY: Decimal("6.00"),
    SubscriptionPlan.MONTHLY: Decimal("24.00"),
    SubscriptionPlan.QUARTERLY: Decimal("72.00"),
}


class NOWPaymentsSubscriptionService:
    """Create and persist a hosted NOWPayments invoice."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.provider = NOWPaymentsService()
        self.repository = NOWPaymentsPaymentRepository(session)

    async def get_available_payment_currencies(
        self, *, plan: SubscriptionPlan
    ) -> list[dict[str, object]]:
        """Return assets whose live minimum for USDC Base is at most $2."""
        price_usd = NOWPAYMENTS_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported subscription plan.")
        return await self.provider.get_available_payment_currencies(price_usd=price_usd)

    async def create_payment(
        self, *, user_id: int, plan: SubscriptionPlan, pay_currency: str
    ) -> tuple[NOWPaymentsPayment, dict[str, object]]:
        """Create and persist a payment in the customer's selected asset."""
        price_usd = NOWPAYMENTS_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported subscription plan.")
        minimum = await self.provider.get_minimum_payment_amount(pay_currency=pay_currency)
        if minimum["fiat_equivalent_usd"] > Decimal("2.00"):
            raise ValueError("The minimum for this asset is currently above $2.")
        if price_usd < minimum["fiat_equivalent_usd"]:
            raise ValueError("The selected subscription price is below the current minimum.")
        order_id = f"NP-{uuid4().hex}"
        provider_payment = await self.provider.create_payment(
            price_usd=price_usd,
            pay_currency=pay_currency,
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
            provider_payment_id=str(provider_payment["payment_id"]),
            pay_address=str(provider_payment["pay_address"]),
            pay_amount=pay_amount,
            payment_status=str(provider_payment.get("payment_status") or "waiting"),
        )
        return payment, provider_payment
