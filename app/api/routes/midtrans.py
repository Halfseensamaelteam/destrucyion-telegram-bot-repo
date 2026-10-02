import hashlib
import hmac
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.db.models.payment import PaymentStatus
from app.db.repositories.payment_repo import PaymentRepository
from app.db.repositories.user_repo import UserRepository
from app.db.session import _get_session_factory
from app.services.payment import PaymentService
from app.services.telegram_notifier import TelegramNotifier

router = APIRouter(prefix="/api/v1", tags=["midtrans"])


def verify_midtrans_signature(payload: dict) -> bool:
    settings = get_settings()
    server_key = settings.midtrans_server_key.get_secret_value()

    order_id = str(payload.get("order_id", ""))
    status_code = str(payload.get("status_code", ""))
    gross_amount = str(payload.get("gross_amount", ""))
    signature_key = str(payload.get("signature_key", ""))

    if not all([order_id, status_code, gross_amount, signature_key, server_key]):
        return False

    raw_signature = f"{order_id}{status_code}{gross_amount}{server_key}"
    expected_signature = hashlib.sha512(
        raw_signature.encode("utf-8")
    ).hexdigest()

    return hmac.compare_digest(expected_signature, signature_key)


def map_midtrans_status(transaction_status: str) -> PaymentStatus:
    status_map = {
        "pending": PaymentStatus.PENDING,
        "settlement": PaymentStatus.SETTLEMENT,
        "expire": PaymentStatus.EXPIRE,
        "cancel": PaymentStatus.CANCEL,
        "deny": PaymentStatus.DENY,
    }

    try:
        return status_map[transaction_status.lower()]
    except KeyError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported Midtrans transaction status: {transaction_status}",
        ) from exc


@router.post("/midtrans/notification")
async def midtrans_notification(request: Request) -> dict:
    payload = await request.json()

    if not verify_midtrans_signature(payload):
        raise HTTPException(
            status_code=401,
            detail="Invalid Midtrans signature.",
        )

    order_id = str(payload.get("order_id", ""))
    if not order_id:
        raise HTTPException(
            status_code=400,
            detail="Missing order_id.",
        )

    try:
        notification_amount = Decimal(
            str(payload.get("gross_amount", ""))
        )
    except InvalidOperation as exc:
        raise HTTPException(
            status_code=400,
            detail="Invalid gross_amount.",
        ) from exc

    transaction_status = str(
        payload.get("transaction_status", "")
    )
    payment_status = map_midtrans_status(transaction_status)
    transaction_id = payload.get("transaction_id")

    session_factory = _get_session_factory()

    async with session_factory() as session:
        payment_repo = PaymentRepository(session)
        payment = await payment_repo.get_by_order_id(order_id)

        if payment is None:
            raise HTTPException(
                status_code=404,
                detail="Payment not found.",
            )

        if notification_amount != Decimal(payment.amount):
            raise HTTPException(
                status_code=400,
                detail="Payment amount mismatch.",
            )

        was_already_settled = (
            payment.status == PaymentStatus.SETTLEMENT
        )

        payment_service = PaymentService(session)

        payment, subscription = await payment_service.process_notification(
            order_id=order_id,
            transaction_id=transaction_id,
            status=payment_status,
        )

        await session.commit()

        should_notify = (
            payment_status == PaymentStatus.SETTLEMENT
            and not was_already_settled
            and subscription is not None
        )

        if should_notify:
            user = await UserRepository(session).get_by_id(
                payment.user_id
            )

            if user is not None:
                notifier = TelegramNotifier()

                await notifier.send_subscription_activated(
                    telegram_user_id=user.telegram_user_id,
                    plan=payment.plan,
                    expires_at=subscription.expires_at,
                )

        return {
            "status": "ok",
            "order_id": payment.order_id,
            "payment_status": payment.status.value,
        }
