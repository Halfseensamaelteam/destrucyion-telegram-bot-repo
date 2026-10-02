"""
app.db.repositories.payment_repo
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Repository for Midtrans payment transactions.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.payment import Payment, PaymentStatus


class PaymentRepository:
    """Database operations for payment transactions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: int,
        order_id: str,
        transaction_id: str | None,
        plan: str,
        amount: int,
        currency: str,
        status: PaymentStatus,
        qr_string: str | None,
        expiry_time: datetime | None,
    ) -> Payment:
        """Create a new payment record."""

        payment = Payment(
            user_id=user_id,
            order_id=order_id,
            transaction_id=transaction_id,
            plan=plan,
            amount=amount,
            currency=currency,
            status=status,
            qr_string=qr_string,
            expiry_time=expiry_time,
        )

        self.session.add(payment)
        await self.session.flush()

        return payment

    async def get_by_order_id(
        self,
        order_id: str,
    ) -> Payment | None:
        """Get a payment by Midtrans order ID."""

        result = await self.session.execute(
            select(Payment).where(
                Payment.order_id == order_id,
            )
        )

        return result.scalar_one_or_none()

    async def get_by_transaction_id(
        self,
        transaction_id: str,
    ) -> Payment | None:
        """Get a payment by Midtrans transaction ID."""

        result = await self.session.execute(
            select(Payment).where(
                Payment.transaction_id == transaction_id,
            )
        )

        return result.scalar_one_or_none()

    async def update_status(
        self,
        payment: Payment,
        status: PaymentStatus,
    ) -> Payment:
        """Update payment status."""

        payment.status = status

        await self.session.flush()

        return payment

    async def update_transaction(
        self,
        payment: Payment,
        *,
        transaction_id: str | None = None,
        status: PaymentStatus | None = None,
    ) -> Payment:
        """Update Midtrans transaction ID and/or payment status."""

        if transaction_id:
            payment.transaction_id = transaction_id

        if status is not None:
            payment.status = status

        await self.session.flush()

        return payment
