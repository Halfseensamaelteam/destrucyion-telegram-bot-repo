"""
app.bot.handlers.start
~~~~~~~~~~~~~~~~~~~~~~
Basic bot commands: /start, /help, /status.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.db.models.user import User
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.db.repositories.subscription_repo import SubscriptionRepository


@with_db_and_user
async def start_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle the /start command.

    Registers the user (done automatically by @with_db_and_user) and sends a welcome message.
    """
    welcome_text = (
        f"Hello {user.first_name}! 👋\n\n"
        "I am your personal Telegram Media Preserver. I securely connect to your Telegram "
        "account and automatically save timed/self-destructing media (and other media) to your Saved Messages.\n\n"
        "To get started, use the /connect command to link your Telegram account.\n\n"
        "Type /help to see all available commands."
    )
    if update.message:
        await update.message.reply_text(welcome_text)


@with_db_and_user
async def help_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle the /help command.

    Lists all available commands. Includes admin commands if the user is an admin.
    """
    from app.core.config import get_settings
    settings = get_settings()

    help_text = (
        "📖 *Available Commands:*\n\n"
        "🔹 /start - Show the welcome message\n"
        "🔹 /help - Show this help message\n"
        "🔹 /status - View your connected accounts and subscription\n"
        "🔹 /connect - Link a new Telegram account\n"
        "🔹 /disconnect - Unlink a Telegram account\n"
        "🔹 /accounts - List your linked accounts in detail\n"
        "🔹 /subscription - View your subscription details\n"
    )

    if user.telegram_user_id in settings.admin_telegram_ids:
        help_text += (
            "\n🛠 *Admin Commands:*\n"
            "🔸 /admin users - View user statistics\n"
            "🔸 /admin subs - Manage subscriptions\n"
        )

    if update.message:
        await update.message.reply_text(help_text, parse_mode="Markdown")


@with_db_and_user
async def status_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle the /status command.

    Displays an overview of the user's connected accounts and active subscription.
    """
    account_repo = TelegramAccountRepository(session)
    sub_repo = SubscriptionRepository(session)

    accounts = await account_repo.list_by_user(user.id)
    subscription = await sub_repo.get_active_for_user(user.id)

    status_text = f"📊 *Your Status Overview*\n\n"

    # Subscription Section
    if subscription:
        status_text += (
            f"👑 *Subscription:* {subscription.tier.name.title()}\n"
            f"📅 *Expires:* {subscription.expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
        )
    else:
        status_text += "👑 *Subscription:* None (Free tier or expired)\n"

    status_text += "\n📱 *Connected Accounts:*\n"

    # Accounts Section
    if not accounts:
        status_text += "No accounts connected yet. Use /connect to link one."
    else:
        for acc in accounts:
            # We don't have real-time RUNNING vs ERROR here without hitting the worker,
            # but we can show the DB status (ACTIVE/BANNED etc).
            status_text += f"- {acc.phone_masked} [{acc.status.value.upper()}]\n"
            
    if update.message:
        await update.message.reply_text(status_text, parse_mode="Markdown")
