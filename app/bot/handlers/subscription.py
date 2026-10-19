"""
app.bot.handlers.subscription
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Subscription commands: /subscription
"""

from datetime import timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.core.config import get_settings
from app.db.models.user import User
from app.db.repositories.subscription_repo import SubscriptionRepository


def _format_idr(amount: int) -> str:
    return f"IDR {amount:,}".replace(",", ".")


@with_db_and_user
async def subscription_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    user: User,
) -> None:
    """Handle /subscription command. Shows current subscription status."""

    settings = get_settings()
    repo = SubscriptionRepository(session)
    subscription = await repo.get_by_user_id(user.id)

    if not subscription:
        text = (
            "⭐ *Subscription Status*\n\n"
            "🎉 Welcome! You're currently on the *Free* plan.\n\n"
            "💎 Upgrade to unlock premium features:\n"
            "✅ Unlimited media saving\n"
            "✅ Faster processing\n"
            "✅ Priority support\n\n"
            "💳 Select a payment method to get started:"
        )

        keyboard = []
        if settings.enable_qris:
            keyboard.append([
                InlineKeyboardButton(
                    "🇮🇩 Indonesia Payment",
                    callback_data="subscription_qris_menu",
                )
            ])
        if settings.enable_nowpayments:
            keyboard.append([
                InlineKeyboardButton(
                    "💠 Crypto (International)",
                    callback_data="nowpayments_menu",
                )
            ])

        if not keyboard:
            text = (
                "⭐ *Subscription Status*\n\n"
                "⚠️ Payments are currently unavailable. Please try again later."
            )

        if update.message:
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard) if keyboard else None,
            )

        return

    if subscription.expires_at is None:
        expires_line = "⏳ *Valid Until:* ♾️ Forever (Lifetime Access)"
    else:
        wib = timezone(timedelta(hours=7))
        expires_wib = subscription.expires_at.astimezone(wib)

        expires_line = (
            f"⏳ *Valid Until:* "
            f"📅 {expires_wib.strftime('%Y-%m-%d %H:%M WIB')}"
        )

    text = (
        "⭐ *Subscription Status*\n\n"
        f"📦 *Plan:* {subscription.plan.value.title()}\n"
        f"🟢 *Status:* {subscription.status.value.title()}\n"
        f"{expires_line}\n\n"
        "🙏 Thank you for being a valued subscriber!\n"
        "💪 Enjoy your premium benefits!"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Upgrade or Extend Plan",
                callback_data="subscription_add_plan",
            )
        ]
    ]

    if update.message:
        await update.message.reply_text(
            text,
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )