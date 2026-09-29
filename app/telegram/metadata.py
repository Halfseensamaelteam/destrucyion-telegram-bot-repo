"""
app.telegram.metadata
~~~~~~~~~~~~~~~~~~~~~
Sender and chat metadata extraction from Telethon objects.

Handles all real-world sender identity cases without confusing them:
  - User with username
  - User without username (first/last name only)
  - User with no name at all (deleted/unknown account)
  - Group/channel context (sender may be the group itself)
  - Anonymous group admin (sender_id may be the chat id)
  - Forwarded messages (keeps original sender)
  - Channel post (no personal sender)

Design rule: sender identity must NEVER be confused between accounts.
Each result is fully self-contained — no shared state.

This module is pure: it takes Telethon TL objects and returns plain dataclasses.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SenderInfo:
    """Resolved information about the sender of a Telegram message.

    All fields are optional because any of them may be absent in practice.

    Attributes:
        telegram_id: Telegram user/channel ID. None for anonymous senders.
        username: @username without the @. None if not set.
        display_name: Human-readable name (first + last, or channel title).
        is_anonymous: True when the sender acted as an anonymous group admin.
        is_channel: True when the message was sent by a channel/linked post.
    """

    telegram_id: int | None
    username: str | None
    display_name: str | None
    is_anonymous: bool
    is_channel: bool

    @property
    def identity_label(self) -> str:
        """A human-readable identity string for use in captions.

        Priority: @username → display_name → ID → "Anonymous" → "Unknown"

        This string is NEVER used as an identifier — only for display.
        """
        if self.is_anonymous:
            return "Anonymous Admin"
        if self.username:
            return f"@{self.username}"
        if self.display_name:
            return self.display_name
        if self.telegram_id:
            return f"ID:{self.telegram_id}"
        return "Unknown"


@dataclass(frozen=True)
class ChatInfo:
    """Resolved information about the chat a message arrived from.

    Attributes:
        chat_id: Telegram chat/peer ID.
        title: Display name of the chat (group title, channel name, or user name).
        username: @username of the chat (if public). None for private chats.
        is_private: True for direct/private messages.
        is_group: True for groups and supergroups.
        is_channel: True for broadcast channels.
    """

    chat_id: int
    title: str | None
    username: str | None
    is_private: bool
    is_group: bool
    is_channel: bool

    @property
    def display_label(self) -> str:
        """A human-readable label for this chat, for use in captions."""
        if self.username:
            return f"@{self.username}"
        if self.title:
            return self.title
        return f"Chat:{self.chat_id}"


def extract_sender(sender_obj) -> SenderInfo:
    """Extract SenderInfo from a Telethon User, Channel, or None object.

    Handles:
      - telethon.tl.types.User (normal user, possibly deleted)
      - telethon.tl.types.Channel (channel/megagroup acting as sender)
      - None (anonymous or missing sender)

    Args:
        sender_obj: The result of event.get_sender() or message.get_sender().

    Returns:
        SenderInfo with all available fields populated.
    """
    if sender_obj is None:
        return SenderInfo(
            telegram_id=None,
            username=None,
            display_name=None,
            is_anonymous=True,
            is_channel=False,
        )

    # Channel or megagroup acting as a sender
    type_name = type(sender_obj).__name__
    if type_name == "Channel":
        return SenderInfo(
            telegram_id=getattr(sender_obj, "id", None),
            username=getattr(sender_obj, "username", None),
            display_name=getattr(sender_obj, "title", None),
            is_anonymous=False,
            is_channel=True,
        )

    # Normal user
    telegram_id = getattr(sender_obj, "id", None)
    username = getattr(sender_obj, "username", None) or None
    first = (getattr(sender_obj, "first_name", None) or "").strip()
    last = (getattr(sender_obj, "last_name", None) or "").strip()
    display_name = " ".join(filter(None, [first, last])) or None

    # Deleted/unknown accounts have no name
    is_deleted = getattr(sender_obj, "deleted", False)
    if is_deleted:
        display_name = "Deleted Account"

    return SenderInfo(
        telegram_id=telegram_id,
        username=username,
        display_name=display_name,
        is_anonymous=False,
        is_channel=False,
    )


def extract_chat(chat_obj, chat_id: int) -> ChatInfo:
    """Extract ChatInfo from a Telethon Chat/Channel/User object.

    Args:
        chat_obj: The result of event.get_chat(). May be None.
        chat_id: Fallback chat ID if chat_obj is unavailable.

    Returns:
        ChatInfo with all available fields populated.
    """
    if chat_obj is None:
        return ChatInfo(
            chat_id=chat_id,
            title=None,
            username=None,
            is_private=False,
            is_group=False,
            is_channel=False,
        )

    type_name = type(chat_obj).__name__
    title: str | None = None
    username: str | None = getattr(chat_obj, "username", None) or None
    is_private = False
    is_group = False
    is_channel = False

    if type_name == "User":
        is_private = True
        first = (getattr(chat_obj, "first_name", None) or "").strip()
        last = (getattr(chat_obj, "last_name", None) or "").strip()
        title = " ".join(filter(None, [first, last])) or None

    elif type_name == "Channel":
        is_megagroup = getattr(chat_obj, "megagroup", False)
        if is_megagroup:
            is_group = True
        else:
            is_channel = True
        title = getattr(chat_obj, "title", None)

    elif type_name in ("Chat", "ChatFull"):
        is_group = True
        title = getattr(chat_obj, "title", None)

    return ChatInfo(
        chat_id=chat_id,
        title=title,
        username=username,
        is_private=is_private,
        is_group=is_group,
        is_channel=is_channel,
    )


def build_caption(
    sender: SenderInfo,
    chat: ChatInfo,
    media_type: str,
    ttl_seconds: int | None = None,
    original_caption: str | None = None,
    captured_at=None,
) -> str:
    """Build a Saved Messages caption for a captured media item.

    Format:
        📥 [Media type] from [sender] in [chat]
        🆔 Sender ID: 123456789 (@username)
        ⏱ Self-destructing (10s) / View once   ← only for timed media
        💬 [original caption]                    ← only if present
        🕐 2026-09-28 16:33 WIB                  ← only if captured_at given

    Args:
        sender: Resolved SenderInfo.
        chat: Resolved ChatInfo.
        media_type: String like "photo", "video", etc.
        ttl_seconds: TTL for self-destructing media. None = not timed.
            Telegram uses the sentinel 0x7FFFFFFF (2147483647) for
            "View Once" media (self-destructs after being viewed, not
            after a fixed duration) — this is displayed as "View once",
            not literally as "(2147483647s)".
        original_caption: The original message caption if any.
        captured_at: Optional timezone-aware datetime of capture. Rendered
            using settings.display_timezone (CLAUDE.md — Telegram exposes
            no per-user timezone, so this is an operator-configured display
            timezone, not a per-viewer automatic one).

    Returns:
        A plain-text caption string.
    """
    type_emoji = {
        "photo": "🖼",
        "video": "🎬",
        "voice": "🎙",
        "video_note": "📹",
        "document": "📄",
        "sticker": "🎭",
    }.get(media_type, "📥")

    lines = [
        f"{type_emoji} {media_type.replace('_', ' ').title()} from "
        f"{sender.identity_label} in {chat.display_label}"
    ]

    if sender.telegram_id is not None:
        id_line = f"🆔 Sender ID: {sender.telegram_id}"
        if sender.username:
            id_line += f" (@{sender.username})"
        lines.append(id_line)

    if ttl_seconds is not None:
        # 0x7FFFFFFF = Telegram's sentinel for "view once" media — it has
        # no fixed duration, it self-destructs after being opened.
        if ttl_seconds >= 0x7FFFFFFF:
            lines.append("⏱ Self-destructing (View once)")
        else:
            lines.append(f"⏱ Self-destructing ({ttl_seconds}s)")

    if original_caption:
        lines.append(f'💬 "{original_caption}"')

    if captured_at is not None:
        lines.append(f"🕐 {format_local_time(captured_at)}")

    return "\n".join(lines)


def format_local_time(dt) -> str:
    """Render a timezone-aware datetime using settings.display_timezone.

    Telegram does not expose a per-user timezone anywhere in its API (Bot
    API or MTProto) — there is no way to automatically know what timezone
    a given Telegram user is in. This renders in a single operator-chosen
    timezone (CLAUDE.md — DISPLAY_TIMEZONE, default UTC), which is the
    closest honest equivalent of "automatic" formatting available.
    """
    from zoneinfo import ZoneInfo

    from app.core.config import get_settings

    tz_name = get_settings().display_timezone
    try:
        local_dt = dt.astimezone(ZoneInfo(tz_name))
        return local_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M:%S UTC")
