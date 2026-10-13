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
from app.core.config import get_settings
from app.db.models.subscription import SubscriptionPlan
from app.db.models.user import User
from app.services.payment import PaymentService


def _format_idr(amount: int) -> str:
    return f"IDR {amount:,}".replace(",", ".")


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
    settings = get_settings()

    if data == "subscription_add_plan":
        keyboard = [
            [InlineKeyboardButton("🇮🇩 QRIS (Rupiah)", callback_data="subscription_qris_menu")],
            [InlineKeyboardButton("💠 Crypto Payment (NOWPayments)", callback_data="nowpayments_menu")],
        ]
        await query.edit_message_text(
            "➕ *Tambahkan Plan*\n\nPilih metode pembayaran:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data == "subscription_qris_menu":
        keyboard = [
            [InlineKeyboardButton(
                f"🗓️ Weekly — {_format_idr(settings.subscription_weekly_price)}",
                callback_data="subscription_buy_weekly",
            )],
            [InlineKeyboardButton(
                f"📅 Monthly — {_format_idr(settings.subscription_monthly_price)}",
                callback_data="subscription_buy_monthly",
            )],
            [InlineKeyboardButton(
                f"👑 Lifetime — {_format_idr(settings.subscription_lifetime_price)}",
                callback_data="subscription_buy_lifetime",
            )],
        ]
        await query.edit_message_text(
            "🇮🇩 *Pembayaran QRIS*\n\nPilih plan yang ingin dibeli:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # NOWPayments menu is separate from the existing IDR/QRIS flow.
    if data == "nowpayments_menu":
        keyboard = [
            [InlineKeyboardButton("🗓️ Weekly — 4 USDT", callback_data="nowpayments_plan_weekly")],
            [InlineKeyboardButton("📅 Monthly — 8 USDT", callback_data="nowpayments_plan_monthly")],
            [InlineKeyboardButton("👑 Lifetime — 300 USDT", callback_data="nowpayments_plan_lifetime")],
        ]
        await query.edit_message_text(
            "💠 *Pembayaran Crypto*\n\n"
            "Pilih paket. Nilai invoice memakai USD sebagai pendekatan USDT; jumlah koin final ditentukan NOWPayments saat invoice dibuat.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    nowpayments_plans = {
        "nowpayments_plan_weekly": SubscriptionPlan.WEEKLY,
        "nowpayments_plan_monthly": SubscriptionPlan.MONTHLY,
        "nowpayments_plan_lifetime": SubscriptionPlan.LIFETIME,
    }
    selected_crypto_plan = nowpayments_plans.get(data)
    if selected_crypto_plan is not None:
        await query.edit_message_text("⏳ Membuat invoice checkout NOWPayments...")
        try:
            from app.services.nowpayments_subscription import NOWPaymentsSubscriptionService
            crypto_service = NOWPaymentsSubscriptionService(session)
            payment, checkout_url = await crypto_service.create_invoice(
                user_id=user.id,
                plan=selected_crypto_plan,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            await query.edit_message_text(
                "❌ Invoice belum dapat dibuat. Periksa konfigurasi NOWPayments dan coba lagi."
            )
            return

        if not checkout_url.startswith("https://"):
            await query.edit_message_text("❌ NOWPayments tidak mengembalikan link checkout HTTPS yang valid.")
            return

        keyboard = [[InlineKeyboardButton("💳 Bayar Sekarang", url=checkout_url)]]
        await query.edit_message_text(
            "💠 *INVOICE PEMBAYARAN CRYPTO*\n\n"
            + "📦 *Paket:* " + selected_crypto_plan.value.title() + "\n"
            + "💵 *Harga acuan:* " + str(payment.price_amount) + " USD (mendekati harga USDT)\n"
            + "🆔 *Order ID:* `" + payment.order_id + "`\n"
            + "⏳ *Status:* Menunggu pembayaran\n\n"
            + "Tekan tombol di bawah untuk membuka checkout resmi NOWPayments. Pilih aset dan jaringan yang tersedia di halaman checkout. "
            + "Subscription hanya aktif setelah pembayaran diverifikasi oleh server.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
            disable_web_page_preview=True,
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
        f"💰 *Jumlah:* {_format_idr(payment.amount)}\n"
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