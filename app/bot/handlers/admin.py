"""
app.bot.handlers.admin
~~~~~~~~~~~~~~~~~~~~~~
Admin commands: /admin users, /admin subs, /admin grant, /admin revoke
"""

from sqlalchemy import func
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user, admin_only
from app.db.models.user import User
from app.db.models.telegram_account import TelegramAccount
from app.db.models.subscription import Subscription, SubscriptionPlan
from app.db.repositories.user_repo import UserRepository
from app.services.subscription import SubscriptionError, SubscriptionService


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
            "- `subs`: View subscription statistics\n"
            "- `grant <telegram_id> <weekly|monthly|lifetime>`: Give a subscription\n"
            "- `revoke <telegram_id>`: Cancel a subscription",
            parse_mode="Markdown"
        )
        return

    subcommand = context.args[0].lower()

    if subcommand == "users":
        await _admin_users(update, context, session)
    elif subcommand == "subs":
        await _admin_subs(update, context, session)
    elif subcommand == "grant":
        await _admin_grant(update, context, session)
    elif subcommand == "revoke":
        await _admin_revoke(update, context, session)
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


async def _find_target_user(
    update: Update, session: AsyncSession, raw_id: str
) -> User | None:
    """Resolve a Telegram numeric ID to an application user, replying if not found."""
    if not raw_id.lstrip("-").isdigit():
        await update.message.reply_text("The Telegram ID must be a number.")
        return None

    target = await UserRepository(session).get_by_telegram_id(int(raw_id))
    if target is None:
        await update.message.reply_text(
            "No user with that Telegram ID. They must send /start to the bot first."
        )
    return target


async def _admin_grant(update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession) -> None:
    """/admin grant <telegram_id> <weekly|monthly|lifetime>"""
    args = context.args[1:]
    valid_plans = ", ".join(p.value for p in SubscriptionPlan)
    if len(args) != 2:
        await update.message.reply_text(
            f"Usage: /admin grant <telegram_id> <plan>\nPlans: {valid_plans}"
        )
        return

    try:
        plan = SubscriptionPlan(args[1].lower())
    except ValueError:
        await update.message.reply_text(f"Unknown plan. Choose one of: {valid_plans}")
        return

    target = await _find_target_user(update, session, args[0])
    if target is None:
        return

    sub = await SubscriptionService(session).grant_subscription(target.id, plan)
    await session.commit()
    await update.message.reply_text(
        f"✅ Granted {plan.value} subscription to {args[0]}.\n"
        f"Expires: {sub.expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        "The worker picks this up within about 30 seconds."
    )


async def _admin_revoke(update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession) -> None:
    """/admin revoke <telegram_id>"""
    args = context.args[1:]
    if len(args) != 1:
        await update.message.reply_text("Usage: /admin revoke <telegram_id>")
        return

    target = await _find_target_user(update, session, args[0])
    if target is None:
        return

    try:
        await SubscriptionService(session).revoke_subscription(target.id)
    except SubscriptionError as exc:
        await update.message.reply_text(f"❌ {exc}")
        return

    await session.commit()
    await update.message.reply_text(
        f"✅ Revoked the subscription of {args[0]}.\n"
        "The worker stops processing their account within about 30 seconds."
    )
