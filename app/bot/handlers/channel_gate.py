"""Gate bot access on membership in the public announcement channel."""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import ApplicationHandlerStop, ContextTypes

CHANNEL_ID = -1003939024010
CHANNEL_URL = "https://t.me/ViserChanel"


def _join_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Gabung Channel", url=CHANNEL_URL)],
        [InlineKeyboardButton("✅ Saya sudah bergabung", callback_data="check_channel_membership")],
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
        await update.callback_query.answer("Gabung ke channel terlebih dahulu.", show_alert=True)
    if message:
        await message.reply_text(
            "🔒 Kamu harus bergabung ke channel announcement sebelum menggunakan bot.\n"
            "Setelah bergabung, tekan *Saya sudah bergabung* untuk memeriksa ulang.",
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
        await query.edit_message_text("✅ Keanggotaan terverifikasi. Silakan gunakan /start untuk membuka menu bot.")
    else:
        await query.edit_message_text(
            "❌ Kamu belum terdeteksi bergabung. Bergabung dahulu lalu tekan tombol pemeriksaan.",
            reply_markup=_join_keyboard(),
        )
