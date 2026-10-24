"""
app.bot.handlers.start
~~~~~~~~~~~~~~~~~~~~~~
Basic bot commands: /start, /help, /status.
"""

from html import escape

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
        f"👋 Hello {user.first_name}!\n\n"
        "🎉 Welcome to your personal Telegram Media Preserver!\n\n"
        "💎 I securely connect to your Telegram account and automatically save "
        "timed/self-destructing media to your Saved Messages.\n\n"
        "✨ *Features:*\n"
        "✅ Auto-save disappearing media\n"
        "✅ Secure & private storage\n"
        "✅ Easy account management\n\n"
        "⚠️ *Terms of Service*\n"
        "This bot is strictly prohibited from criminal acts, extortion, blackmail, "
        "threats, or any illegal activity.\n\n"
        "🔒 By using this bot, you agree to these terms.\n\n"
        "🚀 Get started with /connect to link your account!\n\n"
        "📖 Type /help for all available commands."
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
        "📖 *Available Commands*\n\n"
        "🔹 /start - Show welcome message\n"
        "🔹 /help - Show this help menu\n"
        "🔹 /status - View your accounts & subscription\n"
        "🔹 /connect - Link a Telegram account\n"
        "🔹 /save <link> - Save a post by t.me link (works for private / "
        "restricted channels your account has joined; send an invite link "
        "first if it has not)\n"
        "🔹 /story <link> - Download Telegram stories: one story with "
        "t.me/<username>/s/<id>, or all active stories with t.me/<username>\n"
        "🔹 /capture - Auto-Capture add-on: auto-save protected media "
        "from ONE channel/group you register (paid add-on)\n"
        "🔹 /disconnect - Unlink your account\n"
        "🔹 /accounts - View account details\n"
        "🔹 /subscription - Manage subscription\n"
    )

    if user.telegram_user_id in settings.admin_telegram_ids:
        help_text += (
            "\n🛠 *Admin Commands*\n\n"
            "🔸 /admin users - View user & account stats\n"
            "🔸 /admin subs - View subscription stats\n"
            "🔸 /admin grant <id> <plan> - Grant subscription\n"
            "🔸 /admin revoke <id> - Cancel subscription\n"
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
    subscription = await sub_repo.get_by_user_id(user.id)

    status_text = "📊 <b>Your Status Overview</b>\n\n"

    # Subscription Section
    if subscription:
        expires_line = (
            "♾️ Never (Lifetime)"
            if subscription.expires_at is None
            else subscription.expires_at.strftime("%Y-%m-%d %H:%M UTC")
        )
        status_text += (
            f"👑 <b>Subscription:</b> {escape(str(subscription.plan.value.title()))}\n"
            f"📅 <b>Expires:</b> {escape(expires_line)}\n"
        )
    else:
        status_text += "👑 <b>Subscription:</b> None (Free tier)\n"

    status_text += "\n📱 <b>Connected Accounts:</b>\n\n"

    # Accounts Section
    if not accounts:
        status_text += "❌ No accounts connected.\n\n🚀 Use /connect to link your first account!"
    else:
        for acc in accounts:
            # We don't have real-time RUNNING vs ERROR here without hitting the worker,
            # but we can show the DB status (ACTIVE/BANNED etc).
            phone = escape(str(acc.phone_masked or "Unknown"))
            account_status = escape(str(acc.status.value.upper()))
            status_text += f"- {phone} [{account_status}]\n"

    if update.message:
        await update.message.reply_text(status_text, parse_mode="HTML")