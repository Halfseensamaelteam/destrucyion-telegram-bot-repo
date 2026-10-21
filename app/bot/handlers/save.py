"""User command to save one permitted media post to the connected account's Saved Messages."""
from __future__ import annotations

import asyncio
import json
import uuid

from telegram import Update
from telegram.ext import ContextTypes
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.dependencies import with_db_and_user
from app.core.logging import get_logger
from app.core.redis import get_redis_client
from app.db.models.telegram_account import TelegramAccountStatus
from app.db.models.user import User
from app.db.repositories.telegram_account_repo import TelegramAccountRepository

log = get_logger(__name__)

SAVE_QUEUE_KEY = "destrucyion:save:queue"
SAVE_RESULT_PREFIX = "destrucyion:save:result:"
SAVE_JOB_TIMEOUT_SECONDS = 240


@with_db_and_user
async def save_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session: AsyncSession,
    user: User,
) -> None:
    """Queue /save for the worker's already-managed Telegram account client."""
    message = update.effective_message
    if message is None:
        return

    log.info("save_command_started", user_id=user.id, argument_count=len(context.args or []))
    if not context.args or len(context.args) != 1:
        await message.reply_text(
            "Gunakan format:\n/save https://t.me/channel/123\n\n"
            "Untuk channel privat, gunakan tautan posting (Copy Link) dari Telegram. "
            "Akun yang terhubung harus sudah menjadi anggota channel tersebut. "
            "Posting dengan proteksi konten yang melarang penyimpanan tidak didukung."
        )
        return

    repo = TelegramAccountRepository(session)
    account = await repo.get_by_user_id(user.id)
    if account is None or account.status != TelegramAccountStatus.ACTIVE:
        await message.reply_text(
            "Hubungkan akun Telegram terlebih dahulu dengan /connect, lalu coba /save lagi."
        )
        return
    if not account.session_ciphertext:
        await message.reply_text("Session Telegram tidak tersedia. Jalankan /connect kembali.")
        return

    redis = get_redis_client()
    if redis is None:
        await message.reply_text(
            "❌ Fitur /save membutuhkan Redis agar bot dapat mengirim tugas ke worker. "
            "Periksa konfigurasi REDIS_URL."
        )
        return

    job_id = uuid.uuid4().hex
    status_message = await message.reply_text(
        "⏳ Tugas /save dikirim ke worker. Memeriksa akses posting dan menyimpan media "
        "ke Pesan Tersimpan akun Telegram yang terhubung…"
    )
    payload = json.dumps(
        {
            "job_id": job_id,
            "user_id": int(user.id),
            "account_id": int(account.id),
            "link": context.args[0],
        },
        ensure_ascii=False,
    )

    try:
        await redis.rpush(SAVE_QUEUE_KEY, payload)
        log.info(
            "save_job_queued",
            job_id=job_id,
            user_id=user.id,
            account_id=account.id,
        )

        result_key = f"{SAVE_RESULT_PREFIX}{job_id}"
        for _ in range(SAVE_JOB_TIMEOUT_SECONDS):
            raw_result = await redis.get(result_key)
            if raw_result:
                result = json.loads(raw_result)
                await status_message.edit_text(str(result.get("message") or "Tugas /save selesai."))
                log.info(
                    "save_job_result_received",
                    job_id=job_id,
                    user_id=user.id,
                    account_id=account.id,
                    ok=bool(result.get("ok")),
                )
                return
            await asyncio.sleep(1)

        log.warning(
            "save_job_timed_out",
            job_id=job_id,
            user_id=user.id,
            account_id=account.id,
        )
        await status_message.edit_text(
            "Worker belum mengembalikan hasil setelah 4 menit. Pastikan python -m worker.main "
            "sedang berjalan dan Redis yang dipakai bot serta worker sama, lalu periksa log worker."
        )
    except Exception:
        log.exception(
            "save_job_submission_failed",
            job_id=job_id,
            user_id=user.id,
            account_id=account.id,
        )
        await status_message.edit_text(
            "❌ Tidak dapat mengirim tugas /save ke worker. Periksa koneksi Redis dan log aplikasi."
        )
