"""
app.services.payment
~~~~~~~~~~~~~~~~~~~
Payment service for Midtrans transactions.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.payment import Payment, PaymentStatus
from app.db.models.subscription import Subscription, SubscriptionPlan
from app.db.repositories.payment_repo import PaymentRepository
from app.services.midtrans import MidtransService
from app.services.subscription import SubscriptionService


class PaymentService:
    """Service layer for payment operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.midtrans = MidtransService()
        self.subscription_service = SubscriptionService(session)

    async def create_qris_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> Payment:
        """Create a Midtrans QRIS transaction and save it."""

        transaction = self.midtrans.create_qris_transaction(
            user_id=user_id,
            plan=plan,
        )

        payment = await self.payment_repo.create(
            user_id=user_id,
            order_id=transaction["order_id"],
            transaction_id=transaction["transaction_id"],
            plan=transaction["plan"],
            amount=transaction["amount"],
            currency=transaction["currency"],
            status=PaymentStatus.PENDING,
            qr_string=transaction["qr_string"],
            expiry_time=transaction["expiry_time"],
        )

        return payment

    async def process_notification(
        self,
        *,
        order_id: str,
        transaction_id: str | None,
        status: PaymentStatus,
    ) -> tuple[Payment, Subscription | None]:
        """Process a verified Midtrans payment notification."""

        payment = await self.payment_repo.get_by_order_id(order_id)

        if payment is None:
            raise ValueError(
                f"Payment not found for order_id: {order_id}"
            )

        previous_status = payment.status
        subscription = None

        await self.payment_repo.update_transaction(
            payment,
            transaction_id=transaction_id,
            status=status,
        )

        if (
            status == PaymentStatus.SETTLEMENT
            and previous_status != PaymentStatus.SETTLEMENT
        ):
            plan = SubscriptionPlan(payment.plan)

            subscription = await self.subscription_service.grant_subscription(
                user_id=payment.user_id,
                plan=plan,
            )

        return payment, subscription
