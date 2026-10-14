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
                f"📆 3 Bulan — {_format_idr(settings.subscription_3_monthly_price)}",
                callback_data="subscription_buy_quarterly",
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
            [InlineKeyboardButton("🗓️ Weekly — $6", callback_data="nowpayments_plan_weekly")],
            [InlineKeyboardButton("📅 Monthly — $24", callback_data="nowpayments_plan_monthly")],
            [InlineKeyboardButton("📆 3 Bulan — $72", callback_data="nowpayments_plan_quarterly")],
        ]
        await query.edit_message_text(
            "💠 *Pembayaran Crypto*\n\n"
            "Pilih paket, lalu tentukan aset tujuan (`currency_to`) dan aset yang akan dikirim (`currency_from`).\n"
            "*Harga crypto dapat berbeda dari harga QRIS.*",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    nowpayments_plans = {
        "nowpayments_plan_weekly": SubscriptionPlan.WEEKLY,
        "nowpayments_plan_monthly": SubscriptionPlan.MONTHLY,
        "nowpayments_plan_quarterly": SubscriptionPlan.QUARTERLY,
    }
    selected_crypto_plan = nowpayments_plans.get(data)
    if selected_crypto_plan is not None:
        await query.edit_message_text("⏳ Memeriksa jaringan tujuan yang didukung NOWPayments...")
        try:
            from app.services.nowpayments_subscription import (
                NOWPAYMENTS_PLAN_PRICES_USD,
                NOWPaymentsSubscriptionService,
            )
            crypto_service = NOWPaymentsSubscriptionService(session)
            targets = await crypto_service.get_available_outcome_currencies()
        except Exception:
            await query.edit_message_text("❌ Tidak dapat memeriksa jaringan NOWPayments saat ini. Coba lagi nanti.")
            return
        if not targets:
            await query.edit_message_text("⚠️ Belum ada wallet tujuan yang dikonfigurasi. Tambahkan alamat wallet yang Anda kontrol ke NOWPAYMENTS_PAYOUT_ADDRESSES di .env, lalu restart bot.")
            return
        keyboard = []
        for i in range(0, len(targets), 2):
            pair = targets[i:i + 2]
            keyboard.append([
                InlineKeyboardButton(
                    f"🔹 {item['label']}",
                    callback_data=f"np:target:{selected_crypto_plan.value}:{item['code']}",
                )
                for item in pair
            ])
        keyboard.append([InlineKeyboardButton("🔙 Kembali ke Paket", callback_data="nowpayments_menu")])
        await query.edit_message_text(
            "⚙️ *Pilih Jaringan / Aset Tujuan Transfer*\n\n"
            f"📦 *Paket Dipilih:* {selected_crypto_plan.value.title()} "
            f"(${NOWPAYMENTS_PLAN_PRICES_USD[selected_crypto_plan]} USD)\n"
            "Silakan pilih aset atau stablecoin yang ingin Anda terima. "
            "Pilihan hanya ditampilkan jika kodenya tersedia di NOWPayments.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("np:target:"):
        parts = data.split(":")
        if len(parts) != 4:
            await query.edit_message_text("⚠️ Pilihan tujuan tidak valid. Gunakan /subscription untuk mulai lagi.")
            return
        _, _, plan_value, target_currency = parts
        try:
            selected_crypto_plan = SubscriptionPlan(plan_value)
            from app.services.nowpayments import SUPPORTED_OUTCOME_CURRENCIES
            target_label = SUPPORTED_OUTCOME_CURRENCIES[target_currency]
            from app.services.nowpayments_subscription import NOWPaymentsSubscriptionService
            crypto_service = NOWPaymentsSubscriptionService(session)
            currencies = await crypto_service.get_available_payment_currencies(
                plan=selected_crypto_plan, outcome_currency=target_currency
            )
        except Exception:
            await query.edit_message_text("❌ Tidak dapat memeriksa pasangan mata uang ini. Silakan pilih tujuan lain.")
            return
        if not currencies:
            await query.edit_message_text(
                f"⚠️ Belum ada aset pembayaran dengan minimum sekitar $2 untuk tujuan {target_label}.\n\n"
                "Silakan kembali dan pilih jaringan tujuan lain.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Kembali ke Chain", callback_data=f"nowpayments_plan_{selected_crypto_plan.value}")]]),
            )
            return
        keyboard = []
        for item in currencies:
            prefix = "🌟" if item.get("recommended") else "🔸"
            suffix = " (Rekomendasi)" if item.get("recommended") else ""
            keyboard.append([InlineKeyboardButton(
                f"{prefix} {item['label']} → {target_label} — min ~${item['minimum_usd']:.2f}{suffix}",
                callback_data=f"np:pay:{selected_crypto_plan.value}:{target_currency}:{item['code']}",
            )])
        keyboard.append([InlineKeyboardButton("🔙 Kembali ke Chain", callback_data=f"nowpayments_plan_{selected_crypto_plan.value}")])
        await query.edit_message_text(
            "💳 *Pilih Aset Pembayaran Anda* (`currency_from`)\n\n"
            "*Detail Transaksi:*\n"
            f"• Paket: {selected_crypto_plan.value.title()} (${NOWPAYMENTS_PLAN_PRICES_USD[selected_crypto_plan]} USD)\n"
            f"• Aset Tujuan (`currency_to`): {target_label}\n\n"
            "Pilih aset yang ingin Anda kirim. Minimum dan kurs dihitung secara live dan dapat berubah.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("np:pay:"):
        parts = data.split(":")
        if len(parts) != 5:
            await query.edit_message_text("⚠️ Pilihan pasangan pembayaran tidak valid. Silakan mulai lagi.")
            return
        _, _, plan_value, target_currency, pay_currency = parts
        try:
            selected_crypto_plan = SubscriptionPlan(plan_value)
        except ValueError:
            await query.edit_message_text("⚠️ Paket tidak valid. Silakan mulai lagi.")
            return
        await query.edit_message_text("⏳ Memeriksa minimum terakhir dan membuat pembayaran...")
        try:
            from app.services.nowpayments_subscription import NOWPaymentsSubscriptionService
            crypto_service = NOWPaymentsSubscriptionService(session)
            payment, provider_payment = await crypto_service.create_payment(
                user_id=user.id,
                plan=selected_crypto_plan,
                pay_currency=pay_currency,
                outcome_currency=target_currency,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            await query.edit_message_text(
                "❌ Pembayaran belum dapat dibuat. Minimum mungkin berubah atau pasangan tidak didukung. "
                "Silakan kembali dan pilih aset lain.",
            )
            return
        pay_amount = provider_payment.get("pay_amount")
        pay_address = str(provider_payment.get("pay_address") or "")
        pay_currency_label = str(provider_payment.get("pay_currency") or pay_currency).upper()
        target_label = str(provider_payment.get("payout_currency") or target_currency).upper()
        extra_id = provider_payment.get("payin_extra_id")
        if not pay_address or not pay_amount:
            await query.edit_message_text("❌ NOWPayments tidak mengembalikan detail pembayaran yang lengkap.")
            return
        details = (
            "💠 *PEMBAYARAN CRYPTO*\n\n"
            f"📦 *Paket:* {selected_crypto_plan.value.title()}\n"
            f"💵 *Harga paket:* ${payment.price_amount} USD\n"
            f"🪙 *Aset yang dikirim:* {pay_currency_label}\n"
            f"🎯 *Aset tujuan:* {target_label}\n"
            f"💰 *Jumlah yang harus dikirim:* `{pay_amount} {pay_currency_label}`\n"
            f"📬 *Alamat deposit:* `{pay_address}`\n"
            f"🆔 *Order ID:* `{payment.order_id}`\n"
            "⏳ *Status:* Menunggu pembayaran\n\n"
        )
        if extra_id:
            details += f"🏷️ *Memo/Tag wajib:* `{extra_id}`\n\n"
        details += (
            "Kirim jumlah tepat ke alamat tersebut melalui jaringan yang dipilih. Jangan kirim dari jaringan lain. "
            "Subscription aktif otomatis setelah NOWPayments mengonfirmasi pembayaran selesai."
        )
        await query.edit_message_text(details, parse_mode="Markdown")
        return
    plans = {
        "subscription_buy_weekly": SubscriptionPlan.WEEKLY,
        "subscription_buy_monthly": SubscriptionPlan.MONTHLY,
        "subscription_buy_quarterly": SubscriptionPlan.QUARTERLY,
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