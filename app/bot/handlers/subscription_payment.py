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

    # Hide methods in the menu and reject stale callback buttons if a provider
    # has been disabled in .env while a user still has an old message open.
    qris_actions = {
        "subscription_qris_menu",
        "subscription_buy_weekly",
        "subscription_buy_monthly",
        "subscription_buy_quarterly",
    }
    is_crypto_action = (
        data == "nowpayments_menu"
        or data.startswith("nowpayments_plan_")
        or data.startswith("np:target:")
        or data.startswith("np:pay:")
    )
    if data in qris_actions and not settings.enable_qris:
        await query.edit_message_text(
            "⚠️ QRIS payment is temporarily disabled.\n\n"
            "🔄 Please try another payment method or check back later!"
        )
        return
    if is_crypto_action and not settings.enable_nowpayments:
        await query.edit_message_text(
            "⚠️ Crypto payment is temporarily disabled.\n\n"
            "🔄 Please try another payment method or check back later!"
        )
        return

    if data == "subscription_add_plan":
        keyboard = []
        if settings.enable_qris:
            keyboard.append([
                InlineKeyboardButton("🇮🇩 QRIS (Indonesia Payments)", callback_data="subscription_qris_menu")
            ])
        if settings.enable_nowpayments:
            keyboard.append([
                InlineKeyboardButton("💠 Crypto (International)", callback_data="nowpayments_menu")
            ])
        message = "➕ *Add Plan*\n\n💳 Choose your preferred payment method:"
        if not keyboard:
            message = "⚠️ Payments are currently unavailable.\n\n🔄 Please try again later!"
        await query.edit_message_text(
            message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
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
                f"📆 3 Months — {_format_idr(settings.subscription_3_monthly_price)}",
                callback_data="subscription_buy_quarterly",
            )],
        ]
        await query.edit_message_text(
            "🇮🇩 *QRIS Payment*\n\n"
            "💎 Select the plan that suits you best:\n\n"
            "✨ Instant activation after payment!",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # NOWPayments menu is separate from the existing IDR/QRIS flow.
    if data == "nowpayments_menu":
        keyboard = [
            [InlineKeyboardButton("🗓️ Weekly — $6", callback_data="nowpayments_plan_weekly")],
            [InlineKeyboardButton("📅 Monthly — $24", callback_data="nowpayments_plan_monthly")],
            [InlineKeyboardButton("📆 3 Months — $72", callback_data="nowpayments_plan_quarterly")],
        ]
        await query.edit_message_text(
            "💠 *Crypto Payment*\n\n"
            "🚀 Select a plan, then choose your crypto!\n\n"
            "💡 *Note:* Crypto prices may differ from QRIS prices.\n"
            "✨ Instant activation after confirmation!",
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
        await query.edit_message_text("⏳ Checking supported networks...")
        try:
            from app.services.nowpayments_subscription import (
                NOWPAYMENTS_PLAN_PRICES_USD,
                NOWPaymentsSubscriptionService,
            )
            crypto_service = NOWPaymentsSubscriptionService(session)
            targets = await crypto_service.get_available_outcome_currencies()
        except Exception:
            await query.edit_message_text("❌ Cannot check networks at this time.\n\n🔄 Please try again later!")
            return
        if not targets:
            await query.edit_message_text(
                "⚠️ No destination wallet configured yet.\n\n"
                "🔧 Please contact support to set up payout addresses."
            )
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
        keyboard.append([InlineKeyboardButton("🔙 Back to Plans", callback_data="nowpayments_menu")])
        await query.edit_message_text(
            "⚙️ *Select Destination Network*\n\n"
            f"📦 *Selected Plan:* {selected_crypto_plan.value.title()} "
            f"(${NOWPAYMENTS_PLAN_PRICES_USD[selected_crypto_plan]} USD)\n\n"
            "💰 Choose the asset you want to receive:\n"
            "✨ Options shown are available on NOWPayments.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("np:target:"):
        parts = data.split(":")
        if len(parts) != 4:
            await query.edit_message_text("⚠️ Invalid selection.\n\n🔄 Use /subscription to start over.")
            return
        _, _, plan_value, target_currency = parts
        try:
            selected_crypto_plan = SubscriptionPlan(plan_value)
            from app.services.nowpayments import SUPPORTED_OUTCOME_CURRENCIES
            target_label = SUPPORTED_OUTCOME_CURRENCIES[target_currency]
            from app.services.nowpayments_subscription import (
                NOWPAYMENTS_PLAN_PRICES_USD,
                NOWPaymentsSubscriptionService,
            )
            crypto_service = NOWPaymentsSubscriptionService(session)
            currencies = await crypto_service.get_available_payment_currencies(
                plan=selected_crypto_plan, outcome_currency=target_currency
            )
        except Exception:
            await query.edit_message_text("❌ Cannot check this pair.\n\n🔄 Please choose another destination.")
            return
        if not currencies:
            await query.edit_message_text(
                f"⚠️ No payment asset available for {target_label}.\n\n"
                "🔄 Please select another destination network.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Chain", callback_data=f"nowpayments_plan_{selected_crypto_plan.value}")]]),
            )
            return
        keyboard = []
        for item in currencies:
            prefix = "🌟" if item.get("recommended") else "🔸"
            suffix = " (Recommended)" if item.get("recommended") else ""
            keyboard.append([InlineKeyboardButton(
                f"{prefix} {item['label']} → {target_label} — Fee ~${item['minimum_usd']:.2f}{suffix}",
                callback_data=f"np:pay:{selected_crypto_plan.value}:{target_currency}:{item['code']}",
            )])
        keyboard.append([InlineKeyboardButton("🔙 Back to Chain", callback_data=f"nowpayments_plan_{selected_crypto_plan.value}")])
        await query.edit_message_text(
            "💳 *Select Payment Asset*\n\n"
            "📋 *Transaction Details:*\n"
            f"📦 Plan: {selected_crypto_plan.value.title()} (${NOWPAYMENTS_PLAN_PRICES_USD[selected_crypto_plan]} USD)\n"
            f"🎯 Destination: {target_label}\n\n"
            "💰 Choose the asset you want to send.\n"
            "📊 Rates calculated live by NowPayments.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("np:pay:"):
        parts = data.split(":")
        if len(parts) != 5:
            await query.edit_message_text("⚠️ Invalid payment pair selection. Please start over.")
            return
        _, _, plan_value, target_currency, pay_currency = parts
        try:
            selected_crypto_plan = SubscriptionPlan(plan_value)
        except ValueError:
            await query.edit_message_text("⚠️ Invalid plan.\n\n🔄 Please start over.")
            return
        await query.edit_message_text("⏳ Creating your payment...")
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
        except Exception as exc:
            await session.rollback()
            # The provider client limits this message to a short, safe API error
            # detail. Never include request headers or credentials here.
            error_detail = str(exc).strip()[:300] or exc.__class__.__name__
            await query.edit_message_text(
                "❌ Payment could not be created.\n\n"
                f"📋 Details: {error_detail}\n\n"
                "🔄 Please try another asset or contact support.",
            )
            return
        pay_amount = provider_payment.get("pay_amount")
        pay_address = str(provider_payment.get("pay_address") or "")
        pay_currency_label = str(provider_payment.get("pay_currency") or pay_currency).upper()
        target_label = str(provider_payment.get("payout_currency") or target_currency).upper()
        extra_id = provider_payment.get("payin_extra_id")
        if not pay_address or not pay_amount:
            await query.edit_message_text("❌ NOWPayments did not return complete payment details.")
            return
        details = (
            "💠 *CRYPTO PAYMENT*\n\n"
            f"📦 Plan: {selected_crypto_plan.value.title()}\n"
            f"💵 Price: ${payment.price_amount} USD\n"
            f"🪙 Send: {pay_currency_label}\n"
            f"🎯 Receive: {target_label}\n"
            f"💰 Amount: `{pay_amount} {pay_currency_label}`\n"
            f"📬 Address: `{pay_address}`\n"
            f"🆔 Order ID: `{payment.order_id}`\n"
            "⏳ Status: ⏳ Waiting for payment\n\n"
        )
        if extra_id:
            details += f"🏷️ Required Memo/Tag: `{extra_id}`\n\n"
        details += (
            "⚠️ Send the EXACT amount to the address above.\n"
            "🚫 Do NOT send from a different network.\n\n"
            "✅ Subscription activates automatically after confirmation!"
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
            "⚠️ Invalid option.\n\n🔄 Please use /subscription again."
        )
        return

    await query.edit_message_text(
        "⏳ Processing your payment..."
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
            f"❌ Failed to create payment.\n\n📋 {exc}\n\n🔄 Please try again."
        )
        return

    if not payment.qr_string:
        await query.message.reply_text(
            "⚠️ Payment created, but QRIS code not found.\n\n🔄 Please try again."
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
        expiry_text = "Not provided by Midtrans"

    caption = (
        "💳 *PAYMENT BILL*\n\n"
        f"📦 Plan: {plan.value.title()}\n"
        f"💰 Amount: {_format_idr(payment.amount)}\n"
        f"🆔 Order ID: `{payment.order_id}`\n"
        f"⏳ Status: ⏳ PENDING\n"
        f"⏰ Deadline: {expiry_text}\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "📲 *HOW TO PAY*\n\n"
        "📱 Scan the QRIS code above using\n"
        "💳 your preferred payment app or e-wallet.\n\n"
        "✨ Instant activation after payment!"
    )

    if qr_image_url:
        caption += (
            "🖼️ QR Code Image Link:\n"
            f"{qr_image_url}\n\n"
        )

    caption += (
        "⚠️ Complete payment before the QRIS expires!\n\n"
        "🙏 Thank you for your purchase!"
    )

    await query.message.reply_photo(
        photo=qr_image,
        caption=caption,
        parse_mode="Markdown",
    )