"""Check membership of the announcement channel."""

from telegram import Update
from telegram.ext import ContextTypes

CHANNEL_ID = -1003939024010
CHANNEL_URL = "https://t.me/ViserChanel"

async def channel_membership_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    user = update.effective_user
    if user is None or user.is_bot:
        return True
    member = await context.bot.get_chat_member(CHANNEL_ID, user.id)
    return member.status in ("member", "administrator", "creator") or (member.status == "restricted" and getattr(member, "is_member", False))
