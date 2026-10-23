"""
app.services.payment
~~~~~~~~~~~~~~~~~~~
Payment service for Midtrans transactions.
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.capture import CaptureSubscription
from app.db.models.payment import Payment, PaymentStatus
from app.db.models.subscription import Subscription, SubscriptionPlan
from app.db.repositories.payment_repo import PaymentRepository
from app.services.capture_addon import CaptureAddonService
from app.services.midtrans import MidtransService
from app.services.subscription import SubscriptionService


class PaymentService:
    """Service layer for payment operations."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.payment_repo = PaymentRepository(session)
        self.midtrans = MidtransService()
        self.subscription_service = SubscriptionService(session)
        self.capture_service = CaptureAddonService(session)

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


    async def create_payment_link_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> tuple[Payment, dict]:
        """Create a hosted Midtrans Payment Link and persist its pending order."""
        transaction = self.midtrans.create_payment_link_transaction(
            user_id=user_id,
            plan=plan,
        )
        payment = await self.payment_repo.create(
            user_id=user_id,
            order_id=transaction["order_id"],
            transaction_id=None,
            plan=transaction["plan"],
            amount=transaction["amount"],
            currency=transaction["currency"],
            status=PaymentStatus.PENDING,
            qr_string=None,
            expiry_time=transaction["expiry_time"],
        )
        return payment, transaction

    async def create_bank_transfer_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
        bank: str,
    ) -> tuple[Payment, dict]:
        """Create and persist a BRI, BNI, or CIMB Virtual Account payment."""

        transaction = self.midtrans.create_bank_transfer_transaction(
            user_id=user_id,
            plan=plan,
            bank=bank,
        )

        payment = await self.payment_repo.create(
            user_id=user_id,
            order_id=transaction["order_id"],
            transaction_id=transaction["transaction_id"],
            plan=transaction["plan"],
            amount=transaction["amount"],
            currency=transaction["currency"],
            status=PaymentStatus.PENDING,
            qr_string=None,
            expiry_time=transaction["expiry_time"],
        )

        return payment, transaction

    async def create_mandiri_bill_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> tuple[Payment, dict]:
        """Create and persist a Mandiri E-Channel bill payment."""

        transaction = self.midtrans.create_mandiri_bill_transaction(
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
            qr_string=None,
            expiry_time=transaction["expiry_time"],
        )

        return payment, transaction

    # ------------------------------------------------------------------
    # Auto-Capture add-on purchases — same persistence + webhook flow as
    # subscriptions, but CAP- order ids and add-on activation on settle.
    # ------------------------------------------------------------------
    async def create_capture_qris_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> Payment:
        """Create a Midtrans QRIS transaction for the capture add-on."""

        transaction = self.midtrans.create_capture_qris_transaction(
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

    async def create_capture_payment_link_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> tuple[Payment, dict]:
        """Create a hosted Midtrans Payment Link for the capture add-on."""
        transaction = self.midtrans.create_capture_payment_link_transaction(
            user_id=user_id,
            plan=plan,
        )
        payment = await self.payment_repo.create(
            user_id=user_id,
            order_id=transaction["order_id"],
            transaction_id=None,
            plan=transaction["plan"],
            amount=transaction["amount"],
            currency=transaction["currency"],
            status=PaymentStatus.PENDING,
            qr_string=None,
            expiry_time=transaction["expiry_time"],
        )
        return payment, transaction

    async def create_capture_bank_transfer_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
        bank: str,
    ) -> tuple[Payment, dict]:
        """Create and persist a capture add-on Virtual Account payment."""

        transaction = self.midtrans.create_capture_bank_transfer_transaction(
            user_id=user_id,
            plan=plan,
            bank=bank,
        )

        payment = await self.payment_repo.create(
            user_id=user_id,
            order_id=transaction["order_id"],
            transaction_id=transaction["transaction_id"],
            plan=transaction["plan"],
            amount=transaction["amount"],
            currency=transaction["currency"],
            status=PaymentStatus.PENDING,
            qr_string=None,
            expiry_time=transaction["expiry_time"],
        )

        return payment, transaction

    async def create_capture_mandiri_bill_payment(
        self,
        *,
        user_id: int,
        plan: SubscriptionPlan,
    ) -> tuple[Payment, dict]:
        """Create and persist a capture add-on Mandiri E-Channel payment."""

        transaction = self.midtrans.create_capture_mandiri_bill_transaction(
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
            qr_string=None,
            expiry_time=transaction["expiry_time"],
        )

        return payment, transaction

    async def process_notification(
        self,
        *,
        order_id: str,
        transaction_id: str | None,
        status: PaymentStatus,
    ) -> tuple[Payment, Subscription | CaptureSubscription | None]:
        """Process a verified Midtrans payment notification.

        On first settlement, the granted product depends on the order id:
        CAP- orders activate the Auto-Capture add-on; all other orders
        activate a base subscription, exactly as before. Returns the
        granted record (Subscription or CaptureSubscription) or None.
        """

        payment = await self.payment_repo.get_by_order_id(order_id)

        if payment is None and "-" in order_id:
            parts = order_id.rsplit("-", 1)
            if parts[1].isdigit():
                prefix = parts[0]
                payment = await self.payment_repo.get_by_order_id(prefix)
                if payment is None:
                    payment = await self.payment_repo.get_by_order_id_prefix(prefix)

        if payment is None:
            raise ValueError(
                f"Payment not found for order_id: {order_id}"
            )

        previous_status = payment.status
        granted: Subscription | CaptureSubscription | None = None

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

            if payment.order_id.startswith("CAP-"):
                granted = await self.capture_service.grant_capture_addon(
                    user_id=payment.user_id,
                    plan=plan,
                    order_id=payment.order_id,
                )
            else:
                granted = await self.subscription_service.grant_subscription(
                    user_id=payment.user_id,
                    plan=plan,
                )

        return payment, granted