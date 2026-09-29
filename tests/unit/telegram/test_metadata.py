"""
tests/unit/telegram/test_metadata.py
Unit tests for app.telegram.metadata — sender and chat extraction.

Tests every real-world sender identity case:
  - User with @username
  - User without @username (first/last only)
  - User with first name only
  - User with no name (deleted account)
  - Anonymous group admin (sender=None)
  - Channel as sender
  - Caption building for each media type and TTL variant

None of these tests make any network connection.
"""

import pytest
from unittest.mock import MagicMock

from app.telegram.metadata import (
    SenderInfo,
    ChatInfo,
    extract_sender,
    extract_chat,
    build_caption,
)


# ---------------------------------------------------------------------------
# Mock builders
# ---------------------------------------------------------------------------

def make_user(
    id=12345,
    username=None,
    first_name=None,
    last_name=None,
    deleted=False,
) -> MagicMock:
    user = MagicMock()
    type(user).__name__ = "User"
    user.id = id
    user.username = username
    user.first_name = first_name
    user.last_name = last_name
    user.deleted = deleted
    return user


def make_channel(id=99, username=None, title="Test Channel") -> MagicMock:
    ch = MagicMock()
    type(ch).__name__ = "Channel"
    ch.id = id
    ch.username = username
    ch.title = title
    ch.megagroup = False
    return ch


def make_megagroup(id=88, username=None, title="Test Group") -> MagicMock:
    ch = MagicMock()
    type(ch).__name__ = "Channel"
    ch.id = id
    ch.username = username
    ch.title = title
    ch.megagroup = True
    return ch


def make_chat(id=77, title="Old Group") -> MagicMock:
    chat = MagicMock()
    type(chat).__name__ = "Chat"
    chat.id = id
    chat.title = title
    chat.username = None
    return chat


# ---------------------------------------------------------------------------
# extract_sender: User cases
# ---------------------------------------------------------------------------

def test_sender_user_with_username():
    user = make_user(id=111, username="johndoe", first_name="John", last_name="Doe")
    info = extract_sender(user)

    assert info.telegram_id == 111
    assert info.username == "johndoe"
    assert info.display_name == "John Doe"
    assert info.is_anonymous is False
    assert info.is_channel is False
    assert info.identity_label == "@johndoe"


def test_sender_user_no_username():
    user = make_user(id=222, username=None, first_name="Jane", last_name="Smith")
    info = extract_sender(user)

    assert info.username is None
    assert info.display_name == "Jane Smith"
    assert info.identity_label == "Jane Smith"


def test_sender_user_first_name_only():
    user = make_user(id=333, username=None, first_name="Alice", last_name=None)
    info = extract_sender(user)

    assert info.display_name == "Alice"
    assert info.identity_label == "Alice"


def test_sender_user_no_name_at_all():
    """User with neither first name, last name, nor username."""
    user = make_user(id=444, username=None, first_name=None, last_name=None)
    info = extract_sender(user)

    assert info.display_name is None
    assert info.identity_label == "ID:444"


def test_sender_user_deleted():
    """Deleted accounts show as 'Deleted Account'."""
    user = make_user(id=555, username=None, first_name=None, deleted=True)
    info = extract_sender(user)

    assert info.display_name == "Deleted Account"
    assert info.is_anonymous is False


def test_sender_none_is_anonymous():
    """None sender means anonymous (e.g. anonymous group admin)."""
    info = extract_sender(None)

    assert info.telegram_id is None
    assert info.username is None
    assert info.display_name is None
    assert info.is_anonymous is True
    assert info.identity_label == "Anonymous Admin"


# ---------------------------------------------------------------------------
# extract_sender: Channel cases
# ---------------------------------------------------------------------------

def test_sender_channel_with_username():
    ch = make_channel(id=999, username="newschannel", title="News Channel")
    info = extract_sender(ch)

    assert info.telegram_id == 999
    assert info.username == "newschannel"
    assert info.display_name == "News Channel"
    assert info.is_channel is True
    assert info.is_anonymous is False
    assert info.identity_label == "@newschannel"


def test_sender_channel_no_username():
    ch = make_channel(id=888, username=None, title="Private Channel")
    info = extract_sender(ch)

    assert info.username is None
    assert info.display_name == "Private Channel"
    assert info.identity_label == "Private Channel"


# ---------------------------------------------------------------------------
# extract_chat: Chat type variants
# ---------------------------------------------------------------------------

def test_chat_private_user():
    user = make_user(id=100, username="friend", first_name="Bob", last_name="Jones")
    info = extract_chat(user, chat_id=100)

    assert info.chat_id == 100
    assert info.is_private is True
    assert info.is_group is False
    assert info.is_channel is False
    assert info.title == "Bob Jones"
    assert info.username == "friend"
    assert info.display_label == "@friend"


def test_chat_supergroup():
    group = make_megagroup(id=200, username="mygroup", title="My Group")
    info = extract_chat(group, chat_id=200)

    assert info.is_group is True
    assert info.is_channel is False
    assert info.title == "My Group"
    assert info.display_label == "@mygroup"


def test_chat_broadcast_channel():
    ch = make_channel(id=300, username=None, title="Broadcast Channel")
    info = extract_chat(ch, chat_id=300)

    assert info.is_channel is True
    assert info.is_group is False
    assert info.display_label == "Broadcast Channel"


def test_chat_old_group():
    chat = make_chat(id=400, title="Old Group")
    info = extract_chat(chat, chat_id=400)

    assert info.is_group is True
    assert info.title == "Old Group"
    assert info.display_label == "Old Group"


def test_chat_none_fallback():
    """When chat is unavailable, fall back to bare chat_id."""
    info = extract_chat(None, chat_id=777)

    assert info.chat_id == 777
    assert info.title is None
    assert info.display_label == "Chat:777"


# ---------------------------------------------------------------------------
# build_caption
# ---------------------------------------------------------------------------

def _make_sender(username=None, display_name=None, tid=100):
    return SenderInfo(
        telegram_id=tid,
        username=username,
        display_name=display_name,
        is_anonymous=False,
        is_channel=False,
    )


def _make_chat(username=None, title="Test Chat"):
    return ChatInfo(
        chat_id=100,
        title=title,
        username=username,
        is_private=False,
        is_group=True,
        is_channel=False,
    )


def test_caption_photo_normal():
    sender = _make_sender(username="alice")
    chat = _make_chat(username="testchat")
    caption = build_caption(sender, chat, "photo")

    assert "Photo" in caption
    assert "@alice" in caption
    assert "@testchat" in caption
    assert "⏱" not in caption  # No TTL
    assert "💬" not in caption  # No original caption


def test_caption_video_timed():
    sender = _make_sender(display_name="Bob Smith")
    chat = _make_chat(title="My Group")
    caption = build_caption(sender, chat, "video", ttl_seconds=15)

    assert "Video" in caption
    assert "Bob Smith" in caption
    assert "⏱ Self-destructing (15s)" in caption


def test_caption_voice_with_original_caption():
    sender = _make_sender(username="charlie")
    chat = _make_chat(title="Private Chat")
    caption = build_caption(sender, chat, "voice", original_caption="Hello there!")

    assert "Voice" in caption
    assert '💬 "Hello there!"' in caption


def test_caption_anonymous_sender():
    sender = SenderInfo(
        telegram_id=None,
        username=None,
        display_name=None,
        is_anonymous=True,
        is_channel=False,
    )
    chat = _make_chat(title="Big Group")
    caption = build_caption(sender, chat, "photo")

    assert "Anonymous Admin" in caption


def test_caption_all_media_types():
    """All supported media types should produce a non-empty caption."""
    sender = _make_sender(username="user")
    chat = _make_chat(title="Chat")
    for mt in ["photo", "video", "voice", "video_note", "document", "sticker"]:
        cap = build_caption(sender, chat, mt)
        assert len(cap) > 0
        assert mt.replace("_", " ").title() in cap


# ---------------------------------------------------------------------------
# identity_label edge cases
# ---------------------------------------------------------------------------

def test_identity_label_unknown():
    sender = SenderInfo(
        telegram_id=None,
        username=None,
        display_name=None,
        is_anonymous=False,
        is_channel=False,
    )
    assert sender.identity_label == "Unknown"


def test_identity_label_id_fallback():
    sender = SenderInfo(
        telegram_id=12345,
        username=None,
        display_name=None,
        is_anonymous=False,
        is_channel=False,
    )
    assert sender.identity_label == "ID:12345"


# ---------------------------------------------------------------------------
# build_caption: view-once sentinel, telegram_id, and timestamp
# ---------------------------------------------------------------------------

def test_view_once_sentinel_shown_as_view_once_not_raw_seconds():
    """Regression test: Telegram's 0x7FFFFFFF sentinel for view-once media
    was previously shown literally as "(2147483647s)"."""
    from app.telegram.metadata import ChatInfo, SenderInfo, build_caption

    sender = SenderInfo(telegram_id=1, username="greekwho", display_name=None,
                          is_anonymous=False, is_channel=False)
    chat = ChatInfo(chat_id=2, title=None, username="greekwho", is_group=False, is_channel=False, is_private=False)

    caption = build_caption(sender, chat, "photo", ttl_seconds=2147483647)

    assert "View once" in caption
    assert "2147483647" not in caption


def test_normal_timed_media_shows_seconds():
    from app.telegram.metadata import ChatInfo, SenderInfo, build_caption

    sender = SenderInfo(telegram_id=1, username="greekwho", display_name=None,
                          is_anonymous=False, is_channel=False)
    chat = ChatInfo(chat_id=2, title=None, username="greekwho", is_group=False, is_channel=False, is_private=False)

    caption = build_caption(sender, chat, "photo", ttl_seconds=10)

    assert "(10s)" in caption


def test_caption_includes_sender_telegram_id():
    from app.telegram.metadata import ChatInfo, SenderInfo, build_caption

    sender = SenderInfo(telegram_id=555666, username="greekwho", display_name=None,
                          is_anonymous=False, is_channel=False)
    chat = ChatInfo(chat_id=2, title=None, username="greekwho", is_group=False, is_channel=False, is_private=False)

    caption = build_caption(sender, chat, "photo")

    assert "555666" in caption
    assert "@greekwho" in caption


def test_caption_includes_formatted_capture_time(monkeypatch):
    import datetime

    from app.telegram.metadata import ChatInfo, SenderInfo, build_caption

    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    from app.core.config import get_settings
    get_settings.cache_clear()

    sender = SenderInfo(telegram_id=1, username=None, display_name=None,
                          is_anonymous=False, is_channel=False)
    chat = ChatInfo(chat_id=2, title=None, username=None, is_group=False, is_channel=False, is_private=True)
    when = datetime.datetime(2026, 9, 28, 16, 33, tzinfo=datetime.timezone.utc)

    caption = build_caption(sender, chat, "photo", captured_at=when)

    get_settings.cache_clear()
    assert "2026-09-28 16:33" in caption
