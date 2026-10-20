"""User command to save one permitted media post to the connected account's Saved Messages."""
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
    """Handle /save <t.me/channel/message> and save media to Telegram Saved Messages."""
    message = update.effective_message
    if message is None:
        return
    log.info("save_command_started", user_id=user.id, argument_count=len(context.args or []))
    if not context.args or len(context.args) != 1:
        await message.reply_text(
            "Gunakan format:\n/save https://t.me/channel/123\n\n"
            "Untuk channel privat, salin tautan posting (Copy Link) dari Telegram. "
            "Akun yang terhubung harus sudah menjadi anggota channel tersebut. "
            "Media yang proteksinya melarang penyimpanan tidak dapat disimpan."
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

    status_message = await message.reply_text(
        "⏳ Memeriksa akses posting dan menyimpan media ke Pesan Tersimpan akun Telegram yang terhubung…"
    )
    account_service = AccountService(session)
    try:
        string_session = await account_service.load_session(account.id, user.id)
        api_id, api_hash = account_service.get_account_api_credentials(account)
    except SessionError:
        await status_message.edit_text("Session tidak dapat dibuka. Jalankan /connect kembali.")
        return

    temp_dir = Path(tempfile.mkdtemp(prefix="viserbot-save-"))
    log.info("save_session_loaded", user_id=user.id, account_id=account.id)
    client = TelegramClient(string_session, api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            await status_message.edit_text("Session Telegram sudah tidak valid. Jalankan /connect kembali.")
            return

        result = await download_post_media(client, context.args[0], temp_dir)
        log.info(
            "save_saved_messages_upload_started",
            user_id=user.id,
            account_id=account.id,
            media_kind=result.media_kind,
            file_size_bytes=result.path.stat().st_size,
        )
        # Send with the authenticated user's MTProto client, not the Bot API.
        # "me" is Telegram Saved Messages for this connected user account.
        await client.send_file(
            "me",
            str(result.path),
            caption=f"Disimpan melalui /save dari: {result.source_title}",
        )
        log.info(
            "save_saved_messages_upload_completed",
            user_id=user.id,
            account_id=account.id,
            media_kind=result.media_kind,
        )
        await status_message.edit_text(
            "✅ Media berhasil disimpan ke Pesan Tersimpan (Saved Messages) akun Telegram yang terhubung."
        )
    except SaveLinkError as exc:
        log.warning(
            "save_command_rejected",
            user_id=user.id,
            account_id=account.id,
            reason=str(exc),
        )
        await status_message.edit_text(f"⚠️ {exc}")
    except Exception:
        log.exception("save_command_failed", user_id=user.id, account_id=account.id)
        await status_message.edit_text(
            "❌ Gagal menyimpan media ke Pesan Tersimpan. Periksa log aplikasi untuk detail, lalu coba lagi."
        )
    finally:
        if client.is_connected():
            await client.disconnect()
        shutil.rmtree(temp_dir, ignore_errors=True)
