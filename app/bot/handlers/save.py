"""User command to save one permitted media post from Telegram by link."""
from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from telethon import TelegramClient
from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.dependencies import with_db_and_user
from app.core.logging import get_logger
from app.db.models.telegram_account import TelegramAccountStatus
from app.db.models.user import User
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.account import AccountService, SessionError
from app.services.private_channel_save import SaveLinkError, download_post_media

log = get_logger(__name__)


@with_db_and_user
async def save_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    user: User,
) -> None:
    """Handle /save <t.me/channel/message> with this user's connected account."""
    message = update.effective_message
    if message is None:
        return
    if not context.args or len(context.args) != 1:
        await message.reply_text(
            "Gunakan format:\n/save https://t.me/channel/123\n\n"
            "Untuk channel privat, salin tautan posting (Copy Link) dari Telegram. "
            "Akun yang terhubung harus sudah menjadi anggota channel tersebut."
        )
        return

    repo = TelegramAccountRepository(session)
    account = await repo.get_by_user_id(user.id)
    if account is None or account.status != TelegramAccountStatus.ACTIVE:
        await message.reply_text("Hubungkan akun Telegram terlebih dahulu dengan /connect, lalu coba /save lagi.")
        return
    if not account.session_ciphertext:
        await message.reply_text("Session Telegram tidak tersedia. Jalankan /connect kembali.")
        return

    await message.reply_text("⏳ Memeriksa akses posting dan mengunduh media…")
    account_service = AccountService(session)
    try:
        string_session = await account_service.load_session(account.id, user.id)
        api_id, api_hash = account_service.get_account_api_credentials(account)
    except SessionError:
        await message.reply_text("Session tidak dapat dibuka. Jalankan /connect kembali.")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="viserbot-save-"))
    client = TelegramClient(string_session, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await message.reply_text("Session Telegram sudah tidak valid. Jalankan /connect kembali.")
            return
        result = await download_post_media(client, context.args[0], temp_dir)
        caption = f"✅ Media berhasil disimpan dari: {result.source_title}"
        with result.path.open("rb") as media_file:
            if result.media_kind == "photo":
                await message.reply_photo(photo=media_file, caption=caption)
            elif result.media_kind == "video":
                await message.reply_video(video=media_file, caption=caption)
            else:
                await message.reply_document(document=media_file, caption=caption)
    except SaveLinkError as exc:
        await message.reply_text(f"⚠️ {exc}")
    except Exception:
        log.exception("save_command_failed", user_id=user.id, account_id=account.id)
        await message.reply_text("❌ Gagal menyimpan media karena terjadi kesalahan. Coba lagi nanti.")
    finally:
        if client.is_connected():
            await client.disconnect()
        shutil.rmtree(temp_dir, ignore_errors=True)
