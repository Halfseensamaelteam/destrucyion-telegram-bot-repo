"""
app.bot.handlers.admin
~~~~~~~~~~~~~~~~~~~~~~
Admin commands: /admin users, /admin subs
"""

from sqlalchemy import func
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user, admin_only
from app.db.models.user import User
from app.db.models.telegram_account import TelegramAccount
from app.db.models.subscription import Subscription


@with_db_and_user
@admin_only
async def admin_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /admin. Routes to sub-commands based on arguments."""
    
    if not context.args:
        await update.message.reply_text(
            "🛠 *Admin Commands*\n\n"
            "Usage: `/admin <command>`\n\n"
            "Commands:\n"
            "- `users`: View user and account statistics\n"
            "- `subs`: View subscription statistics",
            parse_mode="Markdown"
        )
        return

    subcommand = context.args[0].lower()

    if subcommand == "users":
        await _admin_users(update, context, session)
    elif subcommand == "subs":
        await _admin_subs(update, context, session)
    else:
        await update.message.reply_text("Unknown admin command.")


async def _admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession) -> None:
    """View user statistics."""
    user_count = await session.scalar(select(func.count()).select_from(User))
    account_count = await session.scalar(select(func.count()).select_from(TelegramAccount))
    
    text = (
        "👥 *User Statistics*\n\n"
        f"Total Users: {user_count}\n"
        f"Total Connected Accounts: {account_count}"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def _admin_subs(update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession) -> None:
    """View subscription statistics."""
    sub_count = await session.scalar(select(func.count()).select_from(Subscription))
    
    text = (
        "💳 *Subscription Statistics*\n\n"
        f"Total Subscriptions (All time): {sub_count}\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
