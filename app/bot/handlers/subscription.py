"""
app.bot.handlers.subscription
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Subscription commands: /subscription
"""

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.db.models.user import User
from app.db.repositories.subscription_repo import SubscriptionRepository


@with_db_and_user
async def subscription_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /subscription command. Shows current active subscription."""
    repo = SubscriptionRepository(session)
    subscription = await repo.get_active_for_user(user.id)

    if not subscription:
        text = (
            "👑 *Subscription Status*\n\n"
            "You do not have an active subscription.\n"
            "You are currently on the Free tier. Upgrade to Premium to connect more accounts and increase storage limits.\n\n"
            "*(Payment gateway integration pending...)*"
        )
    else:
        text = (
            "👑 *Subscription Status*\n\n"
            f"🔹 *Tier:* {subscription.tier.name.title()}\n"
            f"🔹 *Status:* {subscription.status.name.title()}\n"
            f"📅 *Expires:* {subscription.expires_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
            "Thank you for using our service!"
        )

    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")
