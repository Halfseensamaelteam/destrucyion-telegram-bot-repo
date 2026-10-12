"""
Repository for NOWPayments crypto payment records.

This repository is intentionally separate from PaymentRepository, which
continues to own Midtrans QRIS transactions.
"""

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.nowpayments_payment import NOWPaymentsPayment


class NOWPaymentsPaymentRepository:
    """Database operations for NOWPayments payment attempts."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: int,
        order_id: str,
        plan: str,
        price_amount: Decimal,
        pay_currency: str,
        provider_payment_id: str | None = None,
        invoice_id: str | None = None,
        invoice_url: str | None = None,
        pay_address: str | None = None,
        pay_amount: Decimal | None = None,
        payment_status: str = "waiting",
    ) -> NOWPaymentsPayment:
        """Persist a newly created NOWPayments payment/invoice."""
        payment = NOWPaymentsPayment(
            user_id=user_id,
            order_id=order_id,
            plan=plan,
            price_amount=price_amount,
            price_currency="usd",
            pay_currency=pay_currency,
            provider_payment_id=provider_payment_id,
            invoice_id=invoice_id,
            invoice_url=invoice_url,
            pay_address=pay_address,
            pay_amount=pay_amount,
            payment_status=payment_status,
        )
        self.session.add(payment)
        await self.session.flush()
        return payment

    
    async def get_by_order_id(
        self,
        order_id: str,
        *,
        for_update: bool = False,
    ) -> NOWPaymentsPayment | None:
        statement = select(NOWPaymentsPayment).where(
            NOWPaymentsPayment.order_id == order_id
        )
        if for_update:
            statement = statement.with_for_update()
        result = await self.session.execute(statement)
        return result.scalar_one_or_none()


    async def get_by_provider_payment_id(
        self,
        provider_payment_id: str,
    ) -> NOWPaymentsPayment | None:
        result = await self.session.execute(
            select(NOWPaymentsPayment).where(
                NOWPaymentsPayment.provider_payment_id == provider_payment_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_invoice_id(
        self,
        invoice_id: str,
    ) -> NOWPaymentsPayment | None:
        result = await self.session.execute(
            select(NOWPaymentsPayment).where(
                NOWPaymentsPayment.invoice_id == invoice_id
            )
        )
        return result.scalar_one_or_none()

    async def update_provider_details(
        self,
        payment: NOWPaymentsPayment,
        *,
        provider_payment_id: str | None = None,
        invoice_id: str | None = None,
        invoice_url: str | None = None,
        pay_address: str | None = None,
        pay_amount: Decimal | None = None,
        payment_status: str | None = None,
    ) -> NOWPaymentsPayment:
        """Update provider identifiers/details without overwriting absent values."""
        if provider_payment_id is not None:
            payment.provider_payment_id = provider_payment_id
        if invoice_id is not None:
            payment.invoice_id = invoice_id
        if invoice_url is not None:
            payment.invoice_url = invoice_url
        if pay_address is not None:
            payment.pay_address = pay_address
        if pay_amount is not None:
            payment.pay_amount = pay_amount
        if payment_status is not None:
            payment.payment_status = payment_status

        await self.session.flush()
        return payment
