"""NOWPayments IPN callback and automatic subscription activation."""

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import logging

from fastapi import APIRouter, HTTPException, Request

from app.core.config import get_settings
from app.db.models.subscription import SubscriptionPlan
from app.db.repositories.nowpayments_payment_repo import NOWPaymentsPaymentRepository
from app.db.repositories.user_repo import UserRepository
from app.db.session import _get_session_factory
from app.services.nowpayments import NOWPaymentsService
from app.services.subscription import SubscriptionService
from app.services.telegram_notifier import TelegramNotifier

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/nowpayments", tags=["nowpayments"])

_ALLOWED_STATUSES = {
    "waiting", "confirming", "confirmed", "sending", "partially_paid",
    "finished", "failed", "refunded", "expired",
}


@router.post("/notification")
async def nowpayments_notification(request: Request) -> dict[str, object]:
    """Validate IPN, update one transaction, and grant access once on finished."""
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid JSON payload.") from exc
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="Invalid IPN payload.")

    settings = get_settings()
    ipn_secret = settings.nowpayments_ipn_secret.get_secret_value().strip()
    if not ipn_secret:
        log.error("NOWPayments IPN secret is not configured")
        raise HTTPException(status_code=503, detail="NOWPayments IPN is not configured.")

    signature = request.headers.get("x-nowpayments-sig")
    if not NOWPaymentsService.verify_ipn_signature(payload, signature, ipn_secret):
        raise HTTPException(status_code=401, detail="Invalid NOWPayments signature.")

    order_id = str(payload.get("order_id") or "").strip()
    provider_payment_id = str(payload.get("payment_id") or "").strip()
    status = str(payload.get("payment_status") or "").strip().lower()
    if not order_id or not provider_payment_id or status not in _ALLOWED_STATUSES:
        raise HTTPException(status_code=400, detail="Missing or unsupported payment fields.")

    try:
        notified_price = Decimal(str(payload.get("price_amount")))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="Invalid price_amount.") from exc
    price_currency = str(payload.get("price_currency") or "").lower()
    pay_currency = str(payload.get("pay_currency") or "").lower()

    factory = _get_session_factory()
    notify_user_id: int | None = None
    notify_plan: str | None = None
    notify_expiry = None
    async with factory() as session:
        repository = NOWPaymentsPaymentRepository(session)
        payment = await repository.get_by_order_id(order_id, for_update=True)
        if payment is None:
            raise HTTPException(status_code=404, detail="NOWPayments order not found.")

        if payment.provider_payment_id != provider_payment_id:
            raise HTTPException(status_code=400, detail="Payment ID does not match this order.")
        if notified_price != Decimal(payment.price_amount):
            raise HTTPException(status_code=400, detail="Payment price does not match this order.")
        if price_currency != payment.price_currency.lower():
            raise HTTPException(status_code=400, detail="Price currency does not match this order.")
        if pay_currency != payment.pay_currency.lower():
            raise HTTPException(status_code=400, detail="Payment currency does not match this order.")

        if payload.get("pay_amount") is not None and payment.pay_amount is not None:
            try:
                notified_pay_amount = Decimal(str(payload["pay_amount"]))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail="Invalid pay_amount.") from exc
            if notified_pay_amount != Decimal(payment.pay_amount):
                raise HTTPException(status_code=400, detail="Crypto amount does not match this order.")

        already_activated = payment.activated_at is not None
        if not already_activated:
            payment.payment_status = status

        if status == "finished" and not already_activated:
            try:
                plan = SubscriptionPlan(payment.plan)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Stored subscription plan is invalid.") from exc

            subscription = await SubscriptionService(session).grant_subscription(
                user_id=payment.user_id,
                plan=plan,
            )
            payment.activated_at = datetime.now(timezone.utc)
            payment.payment_status = "finished"
            notify_plan = plan.value
            notify_expiry = subscription.expires_at
            user = await UserRepository(session).get_by_id(payment.user_id)
            notify_user_id = user.telegram_user_id if user is not None else None

        await session.commit()

    # Notify only after the transaction is committed. A notification failure
    # must not roll back or duplicate the subscription grant on provider retry.
    if notify_user_id is not None and notify_plan is not None:
        try:
            await TelegramNotifier().send_subscription_activated(
                telegram_user_id=notify_user_id,
                plan=notify_plan,
                expires_at=notify_expiry,
            )
        except Exception:
            log.exception("NOWPayments subscription notification failed", extra={"order_id": order_id})

    return {"status": "ok", "order_id": order_id, "payment_status": status}
