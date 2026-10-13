"""
app.bot.handlers.subscription_payment
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Subscription plan menu callbacks.

Digital subscriptions are not checked out through third-party payment links
inside Telegram. QRIS and crypto checkout must be implemented on an independent
external sales channel, not initiated from this bot.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.core.config import get_settings
from app.db.models.user import User


def _format_idr(amount: int) -> str:
    return f"IDR {amount:,}".replace(",", ".")


@with_db_and_user
async def subscription_payment_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    user: User,
) -> None:
    """Show subscription plans without starting a third-party in-bot checkout."""

    query = update.callback_query
    if query is None:
        return

    await query.answer()
    data = query.data or ""
    settings = get_settings()

    if data in {"subscription_add_plan", "nowpayments_menu"}:
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
                    f"🗓️ 3 Months — {_format_idr(settings.subscription_quarterly_price)}",
                    callback_data="subscription_buy_quarterly",
                )
            ],
        ]
        await query.edit_message_text(
            "⭐ *Pilih Paket Subscription*\n\n"
            "🗓️ Weekly: " + _format_idr(settings.subscription_weekly_price) + "\n"
            "📅 Monthly: " + _format_idr(settings.subscription_monthly_price) + "\n"
            "🗓️ 3 Months: " + _format_idr(settings.subscription_quarterly_price) + "\n\n"
            "Pilih paket untuk melihat status pembelian.",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data in {
        "subscription_buy_weekly",
        "subscription_buy_monthly",
        "subscription_buy_quarterly",
        "subscription_buy_lifetime",
        "nowpayments_plan_weekly",
        "nowpayments_plan_monthly",
        "nowpayments_plan_quarterly",
        "nowpayments_plan_lifetime",
    }:
        await query.edit_message_text(
            "ℹ️ Pembelian subscription digital melalui QRIS atau crypto belum "
            "tersedia langsung di dalam bot Telegram. Bot ini tidak akan membuat "
            "tagihan pihak ketiga dari menu Telegram. Silakan kembali lagi setelah "
            "kanal checkout eksternal yang terpisah siap digunakan."
        )
        return

    await query.edit_message_text(
        "⚠️ Opsi langganan tidak valid. Silakan gunakan /subscription kembali."
    )
