"""
NOWPayments subscription payment orchestration.

This service is separate from PaymentService to preserve the existing Midtrans
QRIS behavior. Prices currently use USD-equivalent amounts because the
NOWPayments invoice API requires a fiat price_currency such as USD.
"""

from decimal import Decimal
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.subscription import SubscriptionPlan
from app.db.models.nowpayments_payment import NOWPaymentsPayment
from app.db.repositories.nowpayments_payment_repo import NOWPaymentsPaymentRepository
from app.services.nowpayments import NOWPaymentsService


# USD-equivalent price menu. These amounts correspond to the agreed USDT menu
# on an approximately 1:1 USD/USDT basis; do not describe this as an exact
# USDT-denominated invoice until that pricing behavior is explicitly accepted.
NOWPAYMENTS_PLAN_PRICES_USD: dict[SubscriptionPlan, Decimal] = {
    SubscriptionPlan.WEEKLY: Decimal("4.00"),
    SubscriptionPlan.MONTHLY: Decimal("8.00"),
    SubscriptionPlan.QUARTERLY: Decimal("20.00"),
}


class NOWPaymentsSubscriptionService:
    """Create and persist a hosted NOWPayments invoice."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.provider = NOWPaymentsService()
        self.repository = NOWPaymentsPaymentRepository(session)

    async def create_invoice(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> tuple[NOWPaymentsPayment, str]:
        """Create a hosted invoice and persist its ID and checkout URL."""
        price_usd = NOWPAYMENTS_PLAN_PRICES_USD.get(plan)
        if price_usd is None:
            raise ValueError("Unsupported subscription plan.")

        order_id = f"NP-{uuid4().hex}"
        provider_invoice = await self.provider.create_invoice(
            price_usd=price_usd,
            order_id=order_id,
            description=f"{plan.value.title()} subscription",
        )

        payment = await self.repository.create(
            user_id=user_id,
            order_id=order_id,
            plan=plan.value,
            price_amount=price_usd,
            # Customer chooses the actual asset on the hosted checkout.
            pay_currency="checkout",
            invoice_id=str(provider_invoice["id"]),
            invoice_url=str(provider_invoice["invoice_url"]),
            payment_status="waiting",
        )
        return payment, str(provider_invoice["invoice_url"])