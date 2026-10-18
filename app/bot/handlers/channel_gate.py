"""Gate bot access on membership in the public announcement channel."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

CHANNEL_ID = -1003939024010
CHANNEL_URL = "https://t.me/ViserChanel"


def _join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Announcement Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ I've Joined", callback_data="check_channel_membership")],
    ])


async def channel_membership_gate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user is None or user.is_bot:
        return
    # The explicit re-check button must reach its dedicated handler.
    if update.callback_query and update.callback_query.data == "check_channel_membership":
        return
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user.id)
        joined = member.status in ("member", "administrator", "creator") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except TelegramError:
        joined = False
    if joined:
        return
    message = update.effective_message
    if update.callback_query:
        await update.callback_query.answer("⚠️ Please join the channel first!", show_alert=True)
    if message:
        await message.reply_text(
            "🔒 *Access Restricted*\n\n"
            "You must join our announcement channel to use this bot.\n\n"
            "📢 Join the channel, then tap the button below to verify!\n\n"
            "✨ It only takes a few seconds!",
            reply_markup=_join_keyboard(), parse_mode="Markdown",
        )
    raise ApplicationHandlerStop


async def check_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user.id)
        joined = member.status in ("member", "administrator", "creator") or (
            member.status == "restricted" and getattr(member, "is_member", False)
        )
    except TelegramError:
        joined = False
    if joined:
        await query.edit_message_text(
            "✅ *Membership Verified!*\n\n"
            "🎉 Welcome! You can now access all bot features.\n\n"
            "🚀 Use /start to get started!"
        )
    else:
        await query.edit_message_text(
            "❌ *Membership Not Detected*\n\n"
            "Please make sure you've joined the channel.\n\n"
            "🔄 Try the verification button again after joining!",
            reply_markup=_join_keyboard(),
        )
