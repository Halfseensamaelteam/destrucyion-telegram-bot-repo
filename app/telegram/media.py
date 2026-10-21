"""
app.telegram.media
~~~~~~~~~~~~~~~~~~
Media type detection and classification for Telegram messages.

Detects: photo, video, document, voice, video note, sticker.
Also detects timed/self-destructing media via TTL field.

Design:
  - Does NOT download anything. Detection is purely based on message attributes.
  - Returns structured MediaInfo dataclasses — no raw Telethon objects leak out.
  - self_destruct detection best-effort only; reported honestly (CLAUDE.md §8).
"""

from __future__ import annotations

from dataclasses import dataclass

from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    Document,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    DocumentAttributeSticker,
    Photo,
)

from app.db.models.media_record import MediaType


@dataclass
class MediaInfo:
    """Parsed media information extracted from a Telegram message.

    Attributes:
        media_type: The detected type of media.
        ttl_seconds: For self-destructing media, the TTL in seconds. None otherwise.
        is_self_destruct: True if the message has a TTL set (timed media).
        caption: The message caption if present.
    """

    media_type: MediaType
    ttl_seconds: int | None
    is_self_destruct: bool
    caption: str | None
    # True when the source chat forbids forwarding ("Restrict saving
    # content" — Telethon exposes it as Message.noforwards). Such media
    # can only be saved by download + re-upload, never by forwarding.
    is_protected: bool = False


def classify_message(message: Message) -> MediaInfo | None:
    """Classify a Telegram message's media type.

    Returns None if the message contains no supported media.

    Detection priority order:
      1. Photo
      2. Document → Video / Voice / VideoNote / Sticker / Document
      3. Unsupported → None

    Important: self-destruct detection is best-effort only.
    Telegram may not always expose the TTL to MTProto clients.
    We report what we see without over-promising capture.
    """
    if message is None or not hasattr(message, "media"):
        return None

    media = message.media

    if media is None:
        return None

    # --- Photo ---
    if isinstance(media, MessageMediaPhoto):
        photo = media.photo
        ttl = getattr(media, "ttl_seconds", None)
        if not isinstance(photo, Photo):
            # photo may be a PhotoEmpty after deletion
            return None
        return MediaInfo(
            media_type=MediaType.PHOTO,
            ttl_seconds=ttl,
            is_self_destruct=ttl is not None,
            caption=getattr(message, "message", None) or None,
            is_protected=getattr(message, "noforwards", False) is True,
        )

    # --- Document-based media ---
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        ttl = getattr(media, "ttl_seconds", None)
        caption = getattr(message, "message", None) or None

        if not isinstance(doc, Document):
            return None

        attrs = doc.attributes if doc.attributes else []
        media_type = _classify_document(attrs)

        return MediaInfo(
            media_type=media_type,
            ttl_seconds=ttl,
            is_self_destruct=ttl is not None,
            caption=caption,
            is_protected=getattr(message, "noforwards", False) is True,
        )

    return None


def _classify_document(attributes: list) -> MediaType:
    """Determine the MediaType from a document's attribute list."""
    for attr in attributes:
        if isinstance(attr, DocumentAttributeAudio):
            if getattr(attr, "voice", False):
                return MediaType.VOICE
            return MediaType.DOCUMENT  # regular audio treated as document

        if isinstance(attr, DocumentAttributeVideo):
            if getattr(attr, "round_message", False):
                return MediaType.VIDEO_NOTE
            return MediaType.VIDEO

        if isinstance(attr, DocumentAttributeSticker):
            return MediaType.STICKER

    return MediaType.DOCUMENT


def has_media(message: Message) -> bool:
    """Quick check: does this message have any capturable media?"""
    return classify_message(message) is not None
