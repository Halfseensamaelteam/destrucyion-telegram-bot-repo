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
            "⭐ *Status Langganan*\n\n"
            "Anda tidak memiliki langganan aktif.\n"
            "Saat ini Anda berada pada paket *Free*.\n\n"
            "Pilih paket Premium di bawah ini:"
        )

        keyboard = [
            [
                InlineKeyboardButton(
                    f"🗓️ Weekly — {_format_idr(settings.subscription_weekly_price)}",
                    callback_data="subscription_buy_weekly",
                )
            ],
            [
                InlineKeyboardButton(
                    f"📅 Monthly — {_format_idr(settings.subscription_monthly_price)}",
                    callback_data="subscription_buy_monthly",
                )
            ],
            [
                InlineKeyboardButton(
                    f"👑 Lifetime — {_format_idr(settings.subscription_lifetime_price)}",
                    callback_data="subscription_buy_lifetime",
                )
            ],
        ]

        if update.message:
            await update.message.reply_text(
                text,
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        return

    if subscription.expires_at is None:
        expires_line = "⏳ *Masa Berlaku:* Selamanya (Akses Lifetime)"
    else:
        wib = timezone(timedelta(hours=7))
        expires_wib = subscription.expires_at.astimezone(wib)

        expires_line = (
            f"⏳ *Masa Berlaku:* "
            f"{expires_wib.strftime('%Y-%m-%d %H:%M WIB')}"
        )

    text = (
        "⭐ *Status Langganan*\n\n"
        f"📦 *Paket:* {subscription.plan.value.title()}\n"
        f"🟢 *Status:* {subscription.status.value.title()}\n"
        f"{expires_line}\n\n"
        "Terima kasih telah menggunakan layanan kami!"
    )

    keyboard = [
        [
            InlineKeyboardButton(
                "➕ Tambahkan Plan",
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
