"""
app.bot.handlers.capture
~~~~~~~~~~~~~~~~~~~~~~~~
/capture — manage the Auto-Capture add-on and its ONE registered channel.

Auto-Capture is a paid add-on (separate from the base subscription):
when active, protected (noforwards) photos & videos posted in the ONE
channel/group the user registers here are auto-saved to their Saved
Messages by the worker. Timed/self-destructing media is unaffected —
it is captured everywhere regardless of this add-on.

Usage:
    /capture                        status, or the paywall if not entitled
    /capture add -1001234567890     register a channel/group (ID only —
                                    links and usernames are rejected;
                                    membership is verified through the
                                    user's own connected account)
    /capture remove -1001234567890  remove the registration

Purchases happen ONLY here (never in /subscription) and reuse the exact
same payment machinery as subscriptions: Midtrans Core (QRIS / bank VA /
Mandiri) and Payment Link orders carry CAP- order ids; NOWPayments
crypto orders carry NPC- order ids. Webhooks activate the add-on based
on those prefixes.

All user-facing copy in this module is English (per product decision).
"""

from __future__ import annotations

import re

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.bot.handlers.subscription_payment import _format_idr
from app.bot.payment_qr_utils import render_payment_qr_png
from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.subscription import SubscriptionPlan
from app.db.models.telegram_account import TelegramAccountStatus
from app.db.models.user import User
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.capture_addon import CaptureAddonService
from app.services.payment import PaymentService
from app.services.subscription import SubscriptionService

log = get_logger(__name__)

_ID_RE = re.compile(r"^-?\d+$")

PAYWALL_TEXT = (
    "🔒 *Auto-Capture is a paid add-on*\n\n"
    "This feature continuously monitors ONE private channel/group you "
    "choose and automatically saves every protected photo & video from "
    "it to your Saved Messages.\n\n"
    "It is not included in your current plan. Activate it below, then "
    "run `/capture add <channel_id>`."
)

SUBSCRIPTION_REQUIRED_TEXT = (
    "🔒 *Auto-Capture is a paid add-on*\n\n"
    "It requires an active subscription first. Subscribe with "
    "/subscription, then come back here to activate Auto-Capture."
)

NO_ACCOUNT_TEXT = (
    "⚠️ You have no connected Telegram account yet. Use /connect first — "
    "Auto-Capture runs on your own account, and it must be a member of "
    "the channel/group you register."
)


def capture_plan_keyboard() -> InlineKeyboardMarkup:
    """The three add-on duration buttons shown in the /capture paywall."""
    settings = get_settings()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            f"🗓️ Weekly — {_format_idr(settings.capture_weekly_price)}",
            callback_data="capture_plan_weekly",
        )],
        [InlineKeyboardButton(
            f"📅 Monthly — {_format_idr(settings.capture_monthly_price)}",
            callback_data="capture_plan_monthly",
        )],
        [InlineKeyboardButton(
            f"📆 3 Months — {_format_idr(settings.capture_3_monthly_price)}",
            callback_data="capture_plan_quarterly",
        )],
    ])


def _parse_chat_id(raw: str) -> tuple[int | None, str | None]:
    """Parse a /capture chat id argument. Returns (chat_id, error)."""
    if "t.me" in raw or "://" in raw or raw.startswith("@"):
        return None, (
            "❌ Links and usernames are not accepted. Send the numeric "
            "chat ID only, e.g. /capture add -1001234567890.\n\n"
            "Tip: forward a message from the channel to a bot like "
            "@userinfobot, or open the channel in Telegram Web, to find "
            "its numeric ID."
        )
    if not _ID_RE.match(raw):
        return None, (
            "❌ That is not a numeric chat ID. Example: "
            "/capture add -1001234567890"
        )
    value = int(raw)
    if value >= 0:
        return None, (
            "❌ That looks like a user ID. Auto-Capture works on "
            "channels/groups only — their IDs are negative and usually "
            "start with -100, e.g. -1001234567890."
        )
    return value, None


@with_db_and_user
async def capture_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /capture, /capture add <id> and /capture remove <id>."""
    if not update.message:
        return

    args = list(context.args or [])
    action = args[0].lower() if args else ""

    if action in ("add", "remove"):
        if len(args) != 2:
            await update.message.reply_text(
                f"❌ Usage: /capture {action} <channel_id>\n"
                "Example: /capture add -1001234567890 (numeric ID only, "
                "no links or usernames)."
            )
            return
        chat_id, error = _parse_chat_id(args[1])
        if error is not None:
            await update.message.reply_text(error)
            return
        if action == "add":
            await _capture_add(update, session, user, chat_id)
        else:
            await _capture_remove(update, session, user, chat_id)
        return

    if action:
        await update.message.reply_text(
            "❌ Unknown option. Use /capture, /capture add <channel_id> "
            "or /capture remove <channel_id>."
        )
        return

    # Bare /capture — status when entitled, paywall otherwise.
    await _capture_status(update, session, user)


async def _capture_status(
    update: Update, session: AsyncSession, user: User
) -> None:
    """Bare /capture: subscription prompt, paywall, or current status."""
    subscription = SubscriptionService(session)
    if not await subscription.check_active(user.id):
        await update.message.reply_text(
            SUBSCRIPTION_REQUIRED_TEXT, parse_mode="Markdown"
        )
        return

    addon = CaptureAddonService(session)
    capture_sub = await addon.get_capture_subscription(user.id)
    if capture_sub is None or not capture_sub.is_active:
        await update.message.reply_text(
            PAYWALL_TEXT,
            parse_mode="Markdown",
            reply_markup=capture_plan_keyboard(),
        )
        return

    account = await TelegramAccountRepository(session).get_by_user_id(user.id)
    if account is None or account.status == TelegramAccountStatus.DISCONNECTED:
        await update.message.reply_text(NO_ACCOUNT_TEXT)
        return

    registration = await addon.get_registered_channel(account.id)
    expires_line = (
        capture_sub.expires_at.strftime("%Y-%m-%d %H:%M UTC")
        if capture_sub.expires_at is not None
        else "Never"
    )
    if registration is None:
        await update.message.reply_text(
            "🛡️ *Auto-Capture is ACTIVE*\n\n"
            f"📅 Expires: {expires_line}\n\n"
            "No channel is registered yet. Register one with:\n"
            "`/capture add <channel_id>`\n\n"
            "The channel ID is numeric only (e.g. -1001234567890), and "
            "your connected account must be a member of it.",
            parse_mode="Markdown",
        )
        return

    await update.message.reply_text(
        "🛡️ *Auto-Capture is ACTIVE*\n\n"
        f"📅 Expires: {expires_line}\n"
        f"📡 Registered channel: `{registration.chat_id}`\n\n"
        "Protected photos & videos from this channel are saved to your "
        "Saved Messages automatically.\n\n"
        "To switch channels, remove this one first:\n"
        f"/capture remove {registration.chat_id}",
        parse_mode="Markdown",
    )


async def _capture_add(
    update: Update, session: AsyncSession, user: User, chat_id: int
) -> None:
    """Handle /capture add <id> — full gate chain, then membership, then store."""
    account = await TelegramAccountRepository(session).get_by_user_id(user.id)
    if account is None or account.status == TelegramAccountStatus.DISCONNECTED:
        await update.message.reply_text(NO_ACCOUNT_TEXT)
        return

    subscription = SubscriptionService(session)
    if not await subscription.check_active(user.id):
        await update.message.reply_text(
            SUBSCRIPTION_REQUIRED_TEXT, parse_mode="Markdown"
        )
        return

    addon = CaptureAddonService(session)
    if not await addon.check_active(user.id):
        await update.message.reply_text(
            PAYWALL_TEXT,
            parse_mode="Markdown",
            reply_markup=capture_plan_keyboard(),
        )
        return

    existing = await addon.get_registered_channel(account.id)
    if existing is not None:
        if existing.chat_id == chat_id:
            await update.message.reply_text(
                f"ℹ️ Channel `{chat_id}` is already registered for "
                "Auto-Capture.",
                parse_mode="Markdown",
            )
        else:
            await update.message.reply_text(
                "❌ You already have a registered channel: "
                f"`{existing.chat_id}`\n\n"
                "Auto-Capture covers ONE channel per account. Remove it "
                "first with:\n"
                f"/capture remove {existing.chat_id}",
                parse_mode="Markdown",
            )
        return

    await update.message.reply_text(
        "⏳ Verifying that your connected account is a member of that "
        "channel/group..."
    )
    is_member, title = await addon.verify_membership(account, chat_id)
    if not is_member:
        await update.message.reply_text(
            "❌ Could not verify membership. Your connected account "
            "must be a member of that channel/group, and the ID must be "
            "correct.\n\n"
            "Join it with your connected account first, then run "
            "/capture add again."
        )
        return

    await addon.register_channel(account.id, chat_id)
    title_line = f" ({title})" if title else ""
    await update.message.reply_text(
        f"✅ Auto-Capture registered for `{chat_id}`{title_line}.\n\n"
        "Protected photos & videos from this channel will now be saved "
        "to your Saved Messages automatically.",
        parse_mode="Markdown",
    )
    log.info("capture_channel_registered", user_id=user.id, account_id=account.id)


async def _capture_remove(
    update: Update, session: AsyncSession, user: User, chat_id: int
) -> None:
    """Handle /capture remove <id> — the id must match the registration.

    Deliberately NOT gated on subscription/add-on state: a user must
    always be able to stop capture, even after the add-on expires.
    """
    account = await TelegramAccountRepository(session).get_by_user_id(user.id)
    if account is None or account.status == TelegramAccountStatus.DISCONNECTED:
        await update.message.reply_text(NO_ACCOUNT_TEXT)
        return

    addon = CaptureAddonService(session)
    existing = await addon.get_registered_channel(account.id)
    if existing is None:
        await update.message.reply_text(
            "ℹ️ You have no registered Auto-Capture channel."
        )
        return

    if existing.chat_id != chat_id:
        await update.message.reply_text(
            "❌ That ID does not match your registered channel: "
            f"`{existing.chat_id}`\n\n"
            f"Remove it with: /capture remove {existing.chat_id}",
            parse_mode="Markdown",
        )
        return

    await addon.remove_channel(account.id)
    await update.message.reply_text(
        f"✅ Auto-Capture channel `{chat_id}` removed. Protected media "
        "from it will no longer be auto-saved.",
        parse_mode="Markdown",
    )
    log.info("capture_channel_removed", user_id=user.id, account_id=account.id)

# ----------------------------------------------------------------------
# Capture add-on payment callbacks — a product-parallel mirror of
# app.bot.handlers.subscription_payment: same providers, same rendering
# style, but capture prices, capture copy, and CAP-/NPC- order ids.
# ----------------------------------------------------------------------

_CAPTURE_PLANS = {
    "weekly": SubscriptionPlan.WEEKLY,
    "monthly": SubscriptionPlan.MONTHLY,
    "quarterly": SubscriptionPlan.QUARTERLY,
}


@with_db_and_user
async def capture_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    user: User,
) -> None:
    """Handle every capture_* payment callback (add-on purchase flow)."""

    query = update.callback_query
    if query is None:
        return

    await query.answer()

    data = query.data or ""
    settings = get_settings()

    # Provider-disabled guards, mirroring subscription_payment_callback:
    # stale buttons on old messages must not create disabled payments.
    is_core_action = (
        data.startswith("capture_v1:")
        or data.startswith("capture_qris:")
        or data.startswith("capture_bank:")
    )
    if is_core_action and not settings.enable_midtrans_core_payments:
        await query.edit_message_text(
            "⚠️ Indonesian bank/QRIS payments are temporarily disabled.\n\n"
            "🔄 Please try another payment method or check back later!"
        )
        return
    if data.startswith("capture_v2:") and not settings.enable_midtrans_payments_link:
        await query.edit_message_text(
            "⚠️ Indonesia Payments V2 is temporarily disabled.\n\n"
            "🔄 Please choose another payment method or check back later!"
        )
        return
    if data.startswith("capture_np") and not settings.enable_nowpayments:
        await query.edit_message_text(
            "⚠️ Crypto payment is temporarily disabled.\n\n"
            "🔄 Please try another payment method or check back later!"
        )
        return

    if data == "capture_paywall":
        await query.edit_message_text(
            PAYWALL_TEXT,
            parse_mode="Markdown",
            reply_markup=capture_plan_keyboard(),
        )
        return

    # Plan chosen → payment-method menu.
    if data.startswith("capture_plan_"):
        plan = _CAPTURE_PLANS.get(data.removeprefix("capture_plan_"))
        if plan is None:
            await query.edit_message_text("⚠️ Invalid option.\n\n🔄 Please use /capture again.")
            return
        keyboard = []
        if settings.enable_midtrans_core_payments:
            keyboard.append([
                InlineKeyboardButton("🇮🇩 Indonesia Payments V1", callback_data=f"capture_v1:{plan.value}")
            ])
        if settings.enable_midtrans_payments_link:
            keyboard.append([
                InlineKeyboardButton("🇮🇩 Indonesia Payments V2", callback_data=f"capture_v2:{plan.value}")
            ])
        if settings.enable_nowpayments:
            keyboard.append([
                InlineKeyboardButton("💠 Crypto (International)", callback_data=f"capture_np_plan:{plan.value}")
            ])
        keyboard.append([
            InlineKeyboardButton("⬅️ Back", callback_data="capture_paywall")
        ])
        message = (
            "🛡️ *Auto-Capture Add-on*\n\n"
            f"📦 Plan: {plan.value.title()} — {_format_idr(PaymentService(session).midtrans._get_capture_price(plan))}\n"
            "💳 Choose your preferred payment method:"
        )
        if len(keyboard) == 1:
            message = "⚠️ Payments are currently unavailable.\n\n🔄 Please try again later!"
            keyboard = []
        await query.edit_message_text(
            message,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
        )
        return

    # V1 → QRIS / bank method selection for the chosen plan.
    if data.startswith("capture_v1:"):
        plan = _CAPTURE_PLANS.get(data.removeprefix("capture_v1:"))
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan. Please start again.")
            return
        keyboard = [
            [InlineKeyboardButton("📱 QRIS", callback_data=f"capture_qris:{plan.value}")],
            [
                InlineKeyboardButton("🏦 BANK BRI", callback_data=f"capture_bank:{plan.value}:bri"),
                InlineKeyboardButton("🏦 BANK BNI", callback_data=f"capture_bank:{plan.value}:bni"),
            ],
            [
                InlineKeyboardButton("🏦 CIMB NIAGA", callback_data=f"capture_bank:{plan.value}:cimb"),
                InlineKeyboardButton("🏦 BANK MANDIRI", callback_data=f"capture_bank:{plan.value}:mandiri"),
            ],
            [InlineKeyboardButton("⬅️ Back to Payment Methods", callback_data=f"capture_plan_{plan.value}")],
        ]
        await query.edit_message_text(
            "🇮🇩 *Indonesia Payment*\n\n"
            f"📦 Selected: Auto-Capture {plan.value.title()} "
            f"({_format_idr(PaymentService(session).midtrans._get_capture_price(plan))})\n"
            "💳 Choose Payment Method:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # V2 → create the hosted payment link immediately (plan already chosen).
    if data.startswith("capture_v2:"):
        plan = _CAPTURE_PLANS.get(data.removeprefix("capture_v2:"))
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan. Please start again.")
            return
        await query.edit_message_text("⏳ Creating your secure Midtrans payment link...")
        payment_service = PaymentService(session)
        try:
            payment, transaction = await payment_service.create_capture_payment_link_payment(
                user_id=user.id,
                plan=plan,
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            error_detail = str(exc).strip()[:300] or exc.__class__.__name__
            await query.edit_message_text(
                "❌ Unable to create your Midtrans payment link.\n\n"
                f"📋 Details: {error_detail}\n\n"
                "No payment has been completed. Please try again or choose Indonesia Payments V1."
            )
            return

        keyboard = [
            [InlineKeyboardButton("🔐 Open Midtrans Secure Checkout", url=transaction["payment_url"])],
            [InlineKeyboardButton("⬅️ Back to Payment Methods", callback_data=f"capture_plan_{plan.value}")],
        ]
        await query.edit_message_text(
            "🇮🇩 *Indonesia Payments V2 — Payment Link*\n\n"
            f"📦 *Product:* Auto-Capture Add-on ({plan.value.title()})\n"
            f"💰 *Amount:* {_format_idr(payment.amount)}\n"
            f"🧾 *Order ID:* `{payment.order_id}`\n"
            "⏳ *Status:* Waiting for payment\n\n"
            "Open the secure Midtrans checkout button below and complete payment there. "
            "Your Auto-Capture add-on will activate automatically after Midtrans confirms the payment.\n\n"
            "⚠️ Do not share this payment link with others.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # Bank VA / Mandiri bill.
    if data.startswith("capture_bank:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.edit_message_text("⚠️ Invalid bank selection. Please start again.")
            return
        _, plan_value, bank = parts
        plan = _CAPTURE_PLANS.get(plan_value)
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan. Please start again.")
            return
        bank_labels = {"bri": "BANK BRI", "bni": "BANK BNI", "cimb": "CIMB NIAGA"}
        payment_service = PaymentService(session)
        if bank == "mandiri":
            await query.edit_message_text("⏳ Creating your Mandiri bill payment...")
            try:
                payment, transaction = await payment_service.create_capture_mandiri_bill_payment(
                    user_id=user.id,
                    plan=plan,
                )
                await session.commit()
            except Exception as exc:
                await session.rollback()
                error_detail = str(exc).strip()[:250] or exc.__class__.__name__
                await query.edit_message_text(
                    "❌ Could not create the Mandiri payment.\n\n"
                    f"📋 Details: {error_detail}\n\n"
                    "🔄 Please try again or choose another payment method."
                )
                return

            biller_code = transaction.get("biller_code")
            bill_key = transaction.get("bill_key")
            if not biller_code or not bill_key:
                await query.edit_message_text(
                    "⚠️ Midtrans created a transaction but did not return the Mandiri biller code and bill key.\n"
                    f"Order ID: `{payment.order_id}`\n"
                    "Please contact support before making a payment."
                )
                return

            expiry_text = (
                payment.expiry_time.strftime("%Y-%m-%d %H:%M WIB")
                if payment.expiry_time is not None
                else "Not provided by Midtrans"
            )
            details = (
                "🏦 *BANK MANDIRI — BILL PAYMENT*\n\n"
                "✅ *Payment created successfully*\n\n"
                f"📦 *Product:* Auto-Capture Add-on ({plan.value.title()})\n"
                f"💰 *Total to pay:* {_format_idr(payment.amount)}\n"
                f"🏢 *Biller Code:* `{biller_code}`\n"
                f"🔑 *Bill Key:* `{bill_key}`\n"
                f"🧾 *Order ID:* `{payment.order_id}`\n"
                "🟡 *Payment status:* Waiting for payment\n"
                f"⏰ *Payment deadline:* {expiry_text}\n\n"
                "━━━━━━━━━━━━━━━━━━━━\n"
                "📲 *HOW TO PAY*\n\n"
                "1. Open Livin' by Mandiri or visit a Mandiri ATM.\n"
                "2. Choose the payment menu for *Multipayment* / *Bayar*.\n"
                "3. Enter the Biller Code shown above.\n"
                "4. Enter the Bill Key shown above.\n"
                "5. Check the displayed amount and billing details, then confirm.\n\n"
                "⚠️ Pay before the deadline and verify the amount before confirming.\n"
                "🎉 Your Auto-Capture add-on activates automatically after Midtrans confirms payment."
            )
            await query.edit_message_text(details, parse_mode="Markdown")
            return

        bank_label = bank_labels.get(bank)
        if bank_label is None:
            await query.edit_message_text("⚠️ Unsupported bank. Please choose BRI, BNI, or CIMB Niaga.")
            return

        await query.edit_message_text("⏳ Creating your Virtual Account...")
        try:
            payment, transaction = await payment_service.create_capture_bank_transfer_payment(
                user_id=user.id,
                plan=plan,
                bank=bank,
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
            error_detail = str(exc).strip()[:250] or exc.__class__.__name__
            await query.edit_message_text(
                "❌ Could not create the bank payment.\n\n"
                f"📋 Details: {error_detail}\n\n"
                "🔄 Please try again or choose another payment method."
            )
            return

        va_number = transaction.get("va_number")
        if not va_number:
            await query.edit_message_text(
                "⚠️ Midtrans created a transaction but did not return a Virtual Account number.\n"
                f"Order ID: `{payment.order_id}`\n"
                "Please contact support before making a payment."
            )
            return

        expiry_text = (
            payment.expiry_time.strftime("%Y-%m-%d %H:%M WIB")
            if payment.expiry_time is not None
            else "Not provided by Midtrans"
        )
        details = (
            f"🏦 *{bank_label} VIRTUAL ACCOUNT*\n\n"
            "✅ *Payment created successfully*\n\n"
            f"📦 *Product:* Auto-Capture Add-on ({plan.value.title()})\n"
            f"💰 *Total to pay:* {_format_idr(payment.amount)}\n"
            f"🔢 *Virtual Account Number:* `{va_number}`\n"
            f"🧾 *Order ID:* `{payment.order_id}`\n"
            "🟡 *Payment status:* Waiting for payment\n"
            f"⏰ *Payment deadline:* {expiry_text}\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "📲 *HOW TO PAY*\n\n"
            "1. Open your banking app or visit an ATM.\n"
            "2. Choose transfer/payment to a Virtual Account.\n"
            "3. Enter the VA number shown above.\n"
            "4. Confirm the recipient and exact amount before paying.\n\n"
            "⚠️ Please pay before the deadline. Do not pay again if you have already completed the transfer.\n"
            "🎉 Your Auto-Capture add-on will activate automatically after Midtrans confirms the payment."
        )
        await query.edit_message_text(details, parse_mode="Markdown")
        return

    # NOWPayments: plan → destination network.
    if data.startswith("capture_np_plan:"):
        plan = _CAPTURE_PLANS.get(data.removeprefix("capture_np_plan:"))
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan. Please start again.")
            return
        await query.edit_message_text("⏳ Checking supported networks...")
        try:
            from app.services.nowpayments_subscription import (
                NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD,
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
                    callback_data=f"capture_np_target:{plan.value}:{item['code']}",
                )
                for item in pair
            ])
        keyboard.append([InlineKeyboardButton("🔙 Back to Payment Methods", callback_data=f"capture_plan_{plan.value}")])
        await query.edit_message_text(
            "⚙️ *Select Destination Network*\n\n"
            f"📦 *Selected Product:* Auto-Capture Add-on ({plan.value.title()}) "
            f"(${NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD[plan]} USD)\n\n"
            "💰 Choose the asset you want to receive:\n"
            "✨ Options shown are available on NOWPayments.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # NOWPayments: destination → payment asset.
    if data.startswith("capture_np_target:"):
        parts = data.split(":")
        if len(parts) != 3:
            await query.edit_message_text("⚠️ Invalid selection.\n\n🔄 Use /capture to start over.")
            return
        _, plan_value, target_currency = parts
        plan = _CAPTURE_PLANS.get(plan_value)
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan.\n\n🔄 Use /capture to start over.")
            return
        try:
            from app.services.nowpayments import SUPPORTED_OUTCOME_CURRENCIES
            target_label = SUPPORTED_OUTCOME_CURRENCIES[target_currency]
            from app.services.nowpayments_subscription import (
                NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD,
                NOWPaymentsSubscriptionService,
            )
            crypto_service = NOWPaymentsSubscriptionService(session)
            currencies = await crypto_service.get_available_capture_payment_currencies(
                plan=plan, outcome_currency=target_currency
            )
        except Exception:
            await query.edit_message_text("❌ Cannot check this pair.\n\n🔄 Please choose another destination.")
            return
        if not currencies:
            await query.edit_message_text(
                f"⚠️ No payment asset available for {target_label}.\n\n"
                "🔄 Please select another destination network.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Back to Chain", callback_data=f"capture_np_plan:{plan.value}")]]),
            )
            return
        keyboard = []
        for item in currencies:
            prefix = "🌟" if item.get("recommended") else "🔸"
            suffix = " (Recommended)" if item.get("recommended") else ""
            keyboard.append([InlineKeyboardButton(
                f"{prefix} {item['label']} → {target_label} — Fee ~${item['minimum_usd']:.2f}{suffix}",
                callback_data=f"capture_np_pay:{plan.value}:{target_currency}:{item['code']}",
            )])
        keyboard.append([InlineKeyboardButton("🔙 Back to Chain", callback_data=f"capture_np_plan:{plan.value}")])
        await query.edit_message_text(
            "💳 *Select Payment Asset*\n\n"
            "📋 *Transaction Details:*\n"
            f"📦 Product: Auto-Capture Add-on ({plan.value.title()}) (${NOWPAYMENTS_CAPTURE_PLAN_PRICES_USD[plan]} USD)\n"
            f"🎯 Destination: {target_label}\n\n"
            "💰 Choose the asset you want to send.\n"
            "📊 Rates calculated live by NowPayments.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    # NOWPayments: create the capture payment (NPC- order id).
    if data.startswith("capture_np_pay:"):
        parts = data.split(":")
        if len(parts) != 4:
            await query.edit_message_text("⚠️ Invalid payment pair selection. Please start over.")
            return
        _, plan_value, target_currency, pay_currency = parts
        plan = _CAPTURE_PLANS.get(plan_value)
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan.\n\n🔄 Please start over.")
            return
        await query.edit_message_text("⏳ Creating your payment...")
        try:
            from app.services.nowpayments_subscription import NOWPaymentsSubscriptionService
            crypto_service = NOWPaymentsSubscriptionService(session)
            payment, provider_payment = await crypto_service.create_capture_payment(
                user_id=user.id,
                plan=plan,
                pay_currency=pay_currency,
                outcome_currency=target_currency,
            )
            await session.commit()
        except Exception as exc:
            await session.rollback()
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
            f"📦 Product: Auto-Capture Add-on ({plan.value.title()})\n"
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
            "✅ Auto-Capture add-on activates automatically after confirmation!"
        )
        await query.edit_message_text(details, parse_mode="Markdown")
        return

    # QRIS — final branch (capture_qris:{plan}).
    if data.startswith("capture_qris:"):
        plan = _CAPTURE_PLANS.get(data.removeprefix("capture_qris:"))
        if plan is None:
            await query.edit_message_text("⚠️ Invalid plan. Please start again.")
            return

        await query.edit_message_text("⏳ Processing your payment...")

        payment_service = PaymentService(session)
        try:
            payment = await payment_service.create_capture_qris_payment(
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
            expiry_text = payment.expiry_time.strftime("%Y-%m-%d %H:%M:%S WIB")
        else:
            expiry_text = "Not provided by Midtrans"

        caption = (
            "💳 *PAYMENT BILL*\n\n"
            f"📦 Product: Auto-Capture Add-on ({plan.value.title()})\n"
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
        return

    await query.edit_message_text(
        "⚠️ Invalid option.\n\n🔄 Please use /capture again."
    )
