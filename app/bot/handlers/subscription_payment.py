"""
app.bot.handlers.subscription_payment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Payment callbacks for subscription purchases.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.bot.payment_qr_utils import render_payment_qr_png
from app.db.models.subscription import SubscriptionPlan
from app.db.models.user import User
from app.services.payment import PaymentService


@with_db_and_user
async def subscription_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    user: User,
) -> None:
    """Handle subscription plan selection and send the QRIS code."""

    query = update.callback_query
    if query is None:
        return

    await query.answer()

    data = query.data or ""

    if data == "subscription_add_plan":
        keyboard = [
            [
                InlineKeyboardButton(
                    "🗓️ Weekly — IDR 8,000",
                    callback_data="subscription_buy_weekly",
                )
            ],
            [
                InlineKeyboardButton(
                    "📅 Monthly — IDR 30,000",
                    callback_data="subscription_buy_monthly",
                )
            ],
            [
                InlineKeyboardButton(
                    "👑 Lifetime — IDR 2,000,000",
                    callback_data="subscription_buy_lifetime",
                )
            ],
        ]

        await query.edit_message_text(
            "➕ *Tambahkan Plan*\n\n"
            "Pilih plan tambahan:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    plans = {
        "subscription_buy_weekly": SubscriptionPlan.WEEKLY,
        "subscription_buy_monthly": SubscriptionPlan.MONTHLY,
        "subscription_buy_lifetime": SubscriptionPlan.LIFETIME,
    }

    plan = plans.get(data)

    if plan is None:
        await query.edit_message_text(
            "⚠️ Opsi langganan tidak valid. Silakan gunakan /subscription kembali."
        )
        return

    await query.edit_message_text(
        "⏳ Memproses pembayaran QRIS Anda..."
    )

    payment_service = PaymentService(session)

    try:
        payment = await payment_service.create_qris_payment(
            user_id=user.id,
            plan=plan,
        )

        await session.commit()

    except Exception as exc:
        await session.rollback()
        await query.edit_message_text(
            f"❌ Gagal membuat pembayaran:\n{exc}"
        )
        return

    if not payment.qr_string:
        await query.message.reply_text(
            "⚠️ Pembayaran dibuat, tetapi kode QRIS tidak ditemukan."
        )
        return

    qr_image = render_payment_qr_png(payment.qr_string)

    qr_image_url = None

    if payment.transaction_id:
        qr_image_url = (
            "https://api.sandbox.midtrans.com/v2/qris/"
            f"{payment.transaction_id}/qr-code"
        )

    if payment.expiry_time is not None:
        expiry_text = payment.expiry_time.strftime(
            "%Y-%m-%d %H:%M:%S WIB"
        )
    else:
        expiry_text = "Tidak disediakan oleh Midtrans"

    caption = (
        "💳 *TAGIHAN PEMBAYARAN*\n\n"
        f"📦 *Plan:* {plan.value.title()}\n"
        f"💰 *Jumlah:* IDR {payment.amount:,}\n"
        f"🆔 *Order ID:* `{payment.order_id}`\n\n"
        "⏳ *Status:* PENDING\n"
        f"⏰ *Batas Waktu:* {expiry_text}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📲 *CARA PEMBAYARAN*\n"
        "Scan kode QRIS di atas menggunakan "
        "aplikasi pembayaran atau e-wallet pilihan Anda.\n\n"
    )

    if qr_image_url:
        caption += (
            "🖼️ *Link Gambar QR Code*\n"
            f"{qr_image_url}\n\n"
        )

    caption += (
        "⚠️ Harap selesaikan pembayaran sebelum "
        "masa berlaku QRIS habis."
    )

    await query.message.reply_photo(
        photo=qr_image,
        caption=caption,
        parse_mode="Markdown",
    )