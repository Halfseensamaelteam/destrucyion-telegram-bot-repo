"""Redis-backed manual-save jobs processed by the persistent Telethon worker.

The bot process only submits jobs. All MTProto access and Saved Messages uploads
happen in the worker through the existing per-account TelegramClientManager.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.core.logging import get_logger
from app.db.models.telegram_account import TelegramAccountStatus
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.private_channel_save import SaveLinkError, download_post_media

log = get_logger(__name__)

SAVE_QUEUE_KEY = "destrucyion:save:queue"
SAVE_RESULT_PREFIX = "destrucyion:save:result:"
SAVE_RESULT_TTL_SECONDS = 900


async def _set_result(redis, job_id: str, *, ok: bool, message: str) -> None:
    payload = json.dumps({"ok": ok, "message": message}, ensure_ascii=False)
    await redis.set(f"{SAVE_RESULT_PREFIX}{job_id}", payload, ex=SAVE_RESULT_TTL_SECONDS)


async def consume_save_jobs(manager: Any, session_factory: Any) -> None:
    """Consume jobs using the existing client for the owning Telegram account."""
    from app.core.redis import get_redis_client

    redis = get_redis_client()
    if redis is None:
        log.error("save_job_consumer_disabled", reason="redis_not_configured")
        return

    log.info("save_job_consumer_started", queue=SAVE_QUEUE_KEY)
    while True:
        try:
            item = await redis.blpop(SAVE_QUEUE_KEY, timeout=2)
            if item is None:
                continue

            _, raw_payload = item
            job_id = "unknown"
            try:
                job = json.loads(raw_payload)
                job_id = str(job["job_id"])
                account_id = int(job["account_id"])
                user_id = int(job["user_id"])
                link = str(job["link"]).strip()
                if not job_id or not link:
                    raise ValueError("missing job id or link")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                log.warning("save_job_invalid_payload", error_type=type(exc).__name__)
                if job_id != "unknown":
                    await _set_result(redis, job_id, ok=False, message="Data tugas /save tidak valid.")
                continue

            temp_dir = Path(tempfile.mkdtemp(prefix="destrucyion-save-"))
            try:
                # Redis is an internal transport, but still verify the job's
                # account ownership against the authoritative database.
                async with session_factory() as db_session:
                    account = await TelegramAccountRepository(db_session).get_by_user_id(user_id)
                    if (
                        account is None
                        or int(account.id) != account_id
                        or account.status != TelegramAccountStatus.ACTIVE
                    ):
                        raise SaveLinkError(
                            "Akun Telegram aktif yang sesuai tidak ditemukan untuk pengguna ini."
                        )

                # Account clients connect asynchronously after manager.start().
                # Give startup/reconnect a short window rather than failing immediately.
                client = None
                for _ in range(30):
                    client = manager.get_client(account_id)
                    if client is not None and client.is_connected():
                        break
                    await asyncio.sleep(1)
                if client is None or not client.is_connected():
                    raise SaveLinkError(
                        "Akun Telegram belum terhubung di worker. Pastikan worker berjalan dan coba lagi."
                    )
                if not await client.is_user_authorized():
                    raise SaveLinkError(
                        "Session akun Telegram tidak valid. Hubungkan kembali akun dengan /connect."
                    )

                result = await download_post_media(client, link, temp_dir)
                await client.send_file(
                    "me",
                    str(result.path),
                    caption=f"Disimpan melalui /save dari: {result.source_title}",
                )
                log.info(
                    "save_job_completed",
                    job_id=job_id,
                    user_id=user_id,
                    account_id=account_id,
                    media_kind=result.media_kind,
                    file_size_bytes=result.path.stat().st_size,
                )
                await _set_result(
                    redis,
                    job_id,
                    ok=True,
                    message="✅ Media berhasil disimpan ke Pesan Tersimpan (Saved Messages) akun Telegram yang terhubung.",
                )
            except SaveLinkError as exc:
                log.warning(
                    "save_job_rejected",
                    job_id=job_id,
                    user_id=user_id,
                    account_id=account_id,
                    reason=str(exc),
                )
                await _set_result(redis, job_id, ok=False, message=f"⚠️ {exc}")
            except Exception as exc:
                log.exception(
                    "save_job_failed",
                    job_id=job_id,
                    user_id=user_id,
                    account_id=account_id,
                    error_type=type(exc).__name__,
                )
                await _set_result(
                    redis,
                    job_id,
                    ok=False,
                    message="❌ Gagal menyimpan media. Periksa log worker untuk detail, lalu coba lagi.",
                )
            finally:
                shutil.rmtree(temp_dir, ignore_errors=True)
        except asyncio.CancelledError:
            log.info("save_job_consumer_stopping")
            raise
        except Exception as exc:
            log.exception("save_job_consumer_error", error_type=type(exc).__name__)
            await asyncio.sleep(1)
