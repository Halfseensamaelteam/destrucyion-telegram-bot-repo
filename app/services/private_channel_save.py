"""Save permitted Telegram channel/group media using the user's own MTProto session.

This follows the Telethon-based flow used by Self-Destructing-Media-Downloader:
parse a post URL, resolve the peer, fetch the message, and download its media.
Telegram content-protection and timed/view-once controls are intentionally respected.
"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, TypeVar
from urllib.parse import urlparse

from telethon import TelegramClient

from app.core.logging import get_logger
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaDocument, MessageMediaPaidMedia, MessageMediaPhoto, PeerChannel

log = get_logger(__name__)

MAX_SAVE_BYTES = 45 * 1024 * 1024
MAX_AUTOMATIC_FLOOD_WAIT_SECONDS = 300
MAX_FLOOD_WAIT_RETRIES = 3
_TME_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}
T = TypeVar("T")


class SaveLinkError(ValueError):
    """A user-facing error for an unsupported or inaccessible save link."""


@dataclass(frozen=True)
class SaveResult:
    path: Path
    filename: str
    media_kind: str
    source_title: str


async def _with_floodwait_retry(operation: Callable[[], Awaitable[T]]) -> T:
    """Retry a Telethon operation after short FloodWait responses."""
    retries = 0
    while True:
        try:
            return await operation()
        except FloodWaitError as exc:
            retries += 1
            if retries > MAX_FLOOD_WAIT_RETRIES or exc.seconds > MAX_AUTOMATIC_FLOOD_WAIT_SECONDS:
                raise SaveLinkError(
                    f"Telegram meminta jeda {exc.seconds} detik. "
                    "Demi menghindari permintaan berulang, coba lagi setelah jeda tersebut."
                ) from exc
            await asyncio.sleep(exc.seconds + 1)


def parse_telegram_post_link(link: str) -> tuple[str | int, int, bool]:
    """Return (entity, message_id, is_private_link) for supported t.me post URLs."""
    parsed = urlparse(link.strip())
    if parsed.scheme not in {"https", "http"} or (parsed.hostname or "").lower() not in _TME_HOSTS:
        raise SaveLinkError(
            "Kirim tautan posting Telegram yang valid, misalnya https://t.me/channel/123."
        )

    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "s":
        parts = parts[1:]

    if len(parts) == 3 and parts[0] == "c":
        try:
            channel_id, message_id = int(parts[1]), int(parts[2])
        except ValueError as exc:
            raise SaveLinkError("Format tautan channel privat tidak valid.") from exc
        if channel_id <= 0 or message_id <= 0:
            raise SaveLinkError("ID channel atau posting tidak valid.")
        return channel_id, message_id, True

    if len(parts) == 2 and re.fullmatch(r"[A-Za-z0-9_]{4,}", parts[0]):
        try:
            message_id = int(parts[1])
        except ValueError as exc:
            raise SaveLinkError("ID posting tidak valid.") from exc
        if message_id <= 0:
            raise SaveLinkError("ID posting tidak valid.")
        return parts[0], message_id, False

    raise SaveLinkError("Tautan harus menuju satu posting, bukan halaman undangan atau profil.")


async def download_post_media(
    client: TelegramClient,
    link: str,
    destination: Path,
) -> SaveResult:
    """Download one ordinary media post visible and permitted to the authenticated account."""
    from telethon.errors import ChannelPrivateError, ChatAdminRequiredError, MessageIdInvalidError
    from telethon.tl.types import MessageMediaWebPage

    entity_ref, message_id, is_private = parse_telegram_post_link(link)
    try:
        entity = await _with_floodwait_retry(
            lambda: client.get_entity(PeerChannel(entity_ref) if is_private else entity_ref)
        )
        message = await _with_floodwait_retry(lambda: client.get_messages(entity, ids=message_id))
    except SaveLinkError:
        raise
    except (ChannelPrivateError, ChatAdminRequiredError) as exc:
        raise SaveLinkError(
            "Akun Telegram yang terhubung tidak memiliki akses ke posting ini. "
            "Pastikan akun sudah bergabung dengan channel/grup privat."
        ) from exc
    except MessageIdInvalidError as exc:
        raise SaveLinkError("ID posting tidak valid atau posting sudah tidak tersedia.") from exc
    except Exception as exc:
        raise SaveLinkError(
            "Posting tidak dapat diakses. Periksa tautan dan pastikan akun yang terhubung "
            "bisa membuka posting tersebut di Telegram."
        ) from exc

    if not message:
        log.warning("save_post_not_found", message_id=message_id, is_private_link=is_private)
        raise SaveLinkError("Posting tidak ditemukan atau sudah dihapus.")

    message_protected = bool(getattr(message, "noforwards", False))
    entity_protected = bool(getattr(entity, "noforwards", False))
    log.info(
        "save_post_access_checked",
        message_id=message_id,
        is_private_link=is_private,
        has_media=bool(getattr(message, "media", None)),
        message_protected=message_protected,
        entity_protected=entity_protected,
    )
    if message_protected or entity_protected:
        log.warning(
            "save_post_blocked_content_protection",
            message_id=message_id,
            is_private_link=is_private,
            message_protected=message_protected,
            entity_protected=entity_protected,
        )
        raise SaveLinkError(
            "Posting ini memakai proteksi konten Telegram. Fitur /save tidak dapat "
            "menyimpan posting yang proteksinya melarang penyimpanan. Periksa pengaturan "
            "'Restrict Saving Content' atau pilih posting yang mengizinkan penyimpanan."
        )

    media = getattr(message, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        raise SaveLinkError("Posting ini tidak berisi media langsung yang didukung.")
    if getattr(message, "ttl_period", None) or getattr(media, "ttl_seconds", None):
        raise SaveLinkError("Media sementara/view-once tidak didukung oleh fitur /save.")
    if isinstance(media, MessageMediaPaidMedia) or "PaidMedia" in type(media).__name__:
        raise SaveLinkError("Media berbayar tidak didukung oleh fitur /save.")
    if not isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        raise SaveLinkError("Jenis media pada posting ini belum didukung.")

    destination.mkdir(parents=True, exist_ok=True)
    log.info(
        "save_media_download_started",
        message_id=message_id,
        is_private_link=is_private,
        media_kind="photo" if isinstance(media, MessageMediaPhoto) else "document",
    )
    try:
        downloaded = await _with_floodwait_retry(
            lambda: client.download_media(message, file=str(destination))
        )
    except SaveLinkError:
        raise
    except Exception as exc:
        log.exception(
            "save_media_download_failed",
            message_id=message_id,
            is_private_link=is_private,
            error_type=type(exc).__name__,
        )
        raise SaveLinkError(
            "Telegram gagal mengunduh media. Pastikan media masih tersedia dan coba lagi."
        ) from exc

    if not downloaded:
        raise SaveLinkError("Telethon tidak mengembalikan file media untuk posting ini.")

    path = Path(downloaded)
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise SaveLinkError("File hasil unduhan tidak dapat dibaca.") from exc
    if size <= 0:
        path.unlink(missing_ok=True)
        raise SaveLinkError("File media yang diunduh kosong.")
    if size > MAX_SAVE_BYTES:
        path.unlink(missing_ok=True)
        raise SaveLinkError("Ukuran media melebihi batas aman 45 MiB untuk pengiriman melalui bot.")

    log.info(
        "save_media_download_completed",
        message_id=message_id,
        is_private_link=is_private,
        file_size_bytes=size,
    )

    kind = "photo" if isinstance(media, MessageMediaPhoto) else "document"
    if isinstance(media, MessageMediaDocument):
        mime = getattr(getattr(media, "document", None), "mime_type", "") or ""
        if mime.startswith("video/"):
            kind = "video"
    title = str(getattr(entity, "title", None) or getattr(entity, "username", None) or "Telegram")
    return SaveResult(path=path, filename=path.name, media_kind=kind, source_title=title)
