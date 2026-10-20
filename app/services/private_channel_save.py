"""Save a permitted Telegram channel/group post using the user's own MTProto session.

Only normal, accessible media is supported. Protected-content and timed/view-once
messages are intentionally rejected; this service does not bypass Telegram controls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from telethon import TelegramClient
from telethon.tl.types import PeerChannel

MAX_SAVE_BYTES = 45 * 1024 * 1024
_TME_HOSTS = {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}


class SaveLinkError(ValueError):
    """A user-facing error for an unsupported or inaccessible save link."""


@dataclass(frozen=True)
class SaveResult:
    path: Path
    filename: str
    media_kind: str
    source_title: str


def parse_telegram_post_link(link: str) -> tuple[str | int, int, bool]:
    """Return (entity, message_id, is_private_link) for supported t.me post URLs."""
    parsed = urlparse(link.strip())
    if parsed.scheme not in {"https", "http"} or (parsed.hostname or "").lower() not in _TME_HOSTS:
        raise SaveLinkError("Kirim tautan posting Telegram yang valid, misalnya https://t.me/channel/123.")
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
    """Download one ordinary media post visible to the authenticated account."""
    from telethon.errors import FloodWaitError
    from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument, MessageMediaPaidMedia

    try:
        entity_ref, message_id, is_private = parse_telegram_post_link(link)
        entity = await client.get_entity(PeerChannel(entity_ref) if is_private else entity_ref)
        message = await client.get_messages(entity, ids=message_id)
    except SaveLinkError:
        raise
    except FloodWaitError as exc:
        raise SaveLinkError(f"Telegram meminta jeda {exc.seconds} detik. Coba lagi nanti.") from exc
    except Exception as exc:
        raise SaveLinkError(
            "Posting tidak dapat diakses oleh akun Telegram yang terhubung. "
            "Pastikan akun tersebut sudah bergabung dengan channel privat dan tautannya benar."
        ) from exc

    if not message:
        raise SaveLinkError("Posting tidak ditemukan atau sudah dihapus.")
    if getattr(message, "noforwards", False) or getattr(entity, "noforwards", False):
        raise SaveLinkError("Posting ini memakai proteksi konten Telegram dan tidak dapat disimpan lewat fitur ini.")
    media = getattr(message, "media", None)
    if media is None:
        raise SaveLinkError("Posting ini tidak berisi media yang didukung.")
    if getattr(message, "ttl_period", None) or getattr(media, "ttl_seconds", None):
        raise SaveLinkError("Media sementara/view-once tidak didukung oleh fitur /save.")
    if isinstance(media, MessageMediaPaidMedia) or "PaidMedia" in type(media).__name__:
        raise SaveLinkError("Media berbayar tidak didukung oleh fitur /save.")
    if not isinstance(media, (MessageMediaPhoto, MessageMediaDocument)):
        raise SaveLinkError("Jenis media pada posting ini belum didukung.")

    destination.mkdir(parents=True, exist_ok=True)
    try:
        downloaded = await client.download_media(message, file=str(destination))
    except Exception as exc:
        raise SaveLinkError("Telegram gagal mengunduh media. Coba lagi nanti.") from exc
    if not downloaded:
        raise SaveLinkError("Telegram tidak mengembalikan file media.")
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

    kind = "photo" if isinstance(media, MessageMediaPhoto) else "document"
    if isinstance(media, MessageMediaDocument):
        mime = getattr(getattr(media, "document", None), "mime_type", "") or ""
        if mime.startswith("video/"):
            kind = "video"
    title = str(getattr(entity, "title", None) or getattr(entity, "username", None) or "Telegram")
    return SaveResult(path=path, filename=path.name, media_kind=kind, source_title=title)
