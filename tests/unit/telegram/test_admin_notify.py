"""
tests/unit/telegram/test_admin_notify.py
Tests for app.telegram.admin_notify — the operator's private-channel
notification with full customer details (per explicit operator decision
to expose full phone number here — see CLAUDE.md).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.telegram.admin_notify import notify_admin_of_capture
from app.telegram.metadata import ChatInfo, SenderInfo

pytestmark = pytest.mark.asyncio


def make_sender():
    return SenderInfo(telegram_id=555, username="greekwho", display_name=None,
                       is_anonymous=False, is_channel=False)


def make_chat():
    return ChatInfo(chat_id=2, title="Some Group", username=None, is_group=True,
                     is_channel=False, is_private=False)


def make_account(phone_masked="628*****890"):
    account = MagicMock()
    account.id = 1
    account.telegram_user_id = 7231326415
    account.username = "gigabasechad"
    account.phone_masked = phone_masked
    return account


def make_account_service(full_phone="6281234567890"):
    svc = MagicMock()
    svc.get_full_phone = MagicMock(return_value=full_phone)
    return svc


async def test_noop_when_admin_chat_not_configured(monkeypatch):
    from app.core.config import get_settings

    # Ambil instance settings yang sedang aktif
    cfg = get_settings()

    # Cukup ubah field atribut Pydantic (huruf kecil) menjadi None
    monkeypatch.setattr(cfg, "admin_notify_chat_id", None)

    with patch("app.telegram.admin_notify.Bot") as MockBot:
        await notify_admin_of_capture(
            account=make_account(),
            sender=make_sender(),
            chat=make_chat(),
            media_type="photo",
            ttl_seconds=10,
            subscription=None,
            account_service=make_account_service(),
        )
        MockBot.assert_not_called()

    get_settings.cache_clear()


async def test_sends_full_phone_and_subscription_to_configured_chat(monkeypatch):
    monkeypatch.setenv("ADMIN_NOTIFY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("BOT_TOKEN", "123:fake")
    from app.core.config import get_settings
    get_settings.cache_clear()

    import app.telegram.admin_notify as mod
    mod._bot_singleton = None  # reset lazy singleton between tests

    subscription = MagicMock()
    subscription.plan.value = "lifetime"
    subscription.status.value = "active"
    subscription.expires_at = None

    mock_bot = AsyncMock()
    with patch("app.telegram.admin_notify.Bot", return_value=mock_bot):
        await notify_admin_of_capture(
            account=make_account(), sender=make_sender(), chat=make_chat(),
            media_type="photo", ttl_seconds=10, subscription=subscription,
            account_service=make_account_service(full_phone="6281234567890"),
        )

    mock_bot.send_message.assert_awaited_once()
    kwargs = mock_bot.send_message.await_args.kwargs
    assert str(kwargs["chat_id"]) == "-1001234567890"
    text = kwargs["text"]
    assert "7231326415" in text          # customer telegram_id
    assert "gigabasechad" in text        # customer username
    assert "6281234567890" in text       # FULL phone, per operator's choice
    assert "greekwho" in text            # sender of the media
    assert "555" in text                 # sender telegram_id
    assert "lifetime" in text.lower()

    mod._bot_singleton = None
    get_settings.cache_clear()


async def test_notify_failure_is_swallowed_not_raised(monkeypatch):
    monkeypatch.setenv("ADMIN_NOTIFY_CHAT_ID", "-1001234567890")
    monkeypatch.setenv("BOT_TOKEN", "123:fake")
    from app.core.config import get_settings
    get_settings.cache_clear()

    import app.telegram.admin_notify as mod
    mod._bot_singleton = None

    from telegram.error import TelegramError

    mock_bot = AsyncMock()
    mock_bot.send_message = AsyncMock(side_effect=TelegramError("chat not found"))
    with patch("app.telegram.admin_notify.Bot", return_value=mock_bot):
        # Must not raise — a notification failure can never break capture.
        await notify_admin_of_capture(
            account=make_account(), sender=make_sender(), chat=make_chat(),
            media_type="photo", ttl_seconds=10, subscription=None,
            account_service=make_account_service(),
        )

    mod._bot_singleton = None
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_notify_admin_of_capture_video_success(tmp_path):
    dummy_file = tmp_path / "video.mp4"
    dummy_file.write_bytes(b"dummy video data")

    mock_bot = AsyncMock()
    mock_account = MagicMock(id=1, username="testuser", telegram_user_id=123)
    mock_sender = MagicMock(telegram_id=456, identity_label="Sender", username="sender")
    mock_chat = MagicMock(display_label="Chat", title="Chat")
    mock_account_service = MagicMock()
    mock_account_service.get_full_phone.return_value = "+1234567890"

    with patch("app.telegram.admin_notify._get_bot", return_value=mock_bot), \
         patch("app.telegram.admin_notify.get_settings") as mock_settings:
        mock_settings.return_value.admin_notify_chat_id = "-100123456789"
        
        await notify_admin_of_capture(
            account=mock_account,
            sender=mock_sender,
            chat=mock_chat,
            media_type="video",
            ttl_seconds=10,
            subscription=None,
            account_service=mock_account_service,
            file_path=str(dummy_file),
        )

        assert mock_bot.send_video.called
        _, kwargs = mock_bot.send_video.call_args
        assert kwargs["read_timeout"] == 300.0
        assert kwargs["write_timeout"] == 300.0


@pytest.mark.asyncio
async def test_notify_admin_of_capture_retry_on_timeout():
    mock_bot = AsyncMock()
    from telegram.error import TimedOut
    mock_bot.send_message.side_effect = [TimedOut("Timeout"), None]
    mock_account = MagicMock(id=1, username="testuser", telegram_user_id=123)
    mock_sender = MagicMock(telegram_id=456, identity_label="Sender")
    mock_chat = MagicMock(display_label="Chat")
    mock_account_service = MagicMock()

    with patch("app.telegram.admin_notify._get_bot", return_value=mock_bot), \
         patch("app.telegram.admin_notify.get_settings") as mock_settings, \
         patch("asyncio.sleep", new_callable=AsyncMock):
        mock_settings.return_value.admin_notify_chat_id = "-100123456789"
        
        await notify_admin_of_capture(
            account=mock_account,
            sender=mock_sender,
            chat=mock_chat,
            media_type="text",
            ttl_seconds=None,
            subscription=None,
            account_service=mock_account_service,
            file_path=None,
        )

        assert mock_bot.send_message.call_count == 2
