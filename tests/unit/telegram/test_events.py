"""
tests/unit/telegram/test_events.py
Tests for app.telegram.events — automatic + manual (.saveit) capture.

events.py is the heart of the product and previously had NO tests, which is
how a missing incoming-only filter and a missing timed-only filter shipped.
All Telethon / DB access is mocked.
"""

import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.db.models.media_record import MediaRecordStatus, MediaType
from app.telegram import events as ev
from app.telegram.media import MediaInfo

pytestmark = pytest.mark.asyncio


def timed_info():
    return MediaInfo(media_type=MediaType.PHOTO, ttl_seconds=10, is_self_destruct=True, caption=None)


def normal_info():
    return MediaInfo(media_type=MediaType.PHOTO, ttl_seconds=None, is_self_destruct=False, caption=None)


def make_manager(client=None, user_id=7):
    manager = MagicMock()
    managed = MagicMock()
    managed.user_id = user_id
    manager._clients = {1: managed}
    manager.get_client = MagicMock(return_value=client)
    return manager


def make_session_factory():
    """Async-context-manager session factory whose commit is awaitable."""
    session = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return MagicMock(return_value=ctx)


# ---------------------------------------------------------------------------
# Handler registration — the filters that prevent an infinite re-save loop
# ---------------------------------------------------------------------------

async def test_factory_registers_incoming_only_automatic_handler():
    factory = ev.make_handler_factory(make_session_factory())
    handlers = factory(make_manager(), 1)

    _, auto_builder = handlers[0]
    # incoming=True is what stops the worker re-capturing the copy it just
    # uploaded to Saved Messages (an OUTGOING message) forever.
    assert auto_builder.incoming is True


async def test_factory_registers_owner_only_manual_trigger():
    factory = ev.make_handler_factory(make_session_factory())
    handlers = factory(make_manager(), 1)

    assert len(handlers) == 2
    _, manual_builder = handlers[1]
    assert manual_builder.outgoing is True  # only the account owner
    assert manual_builder.pattern is not None
    assert manual_builder.pattern(".saveit")
    assert not manual_builder.pattern(".saveit please")
    assert not manual_builder.pattern("hello .saveit")


async def test_trigger_can_be_disabled_with_empty_setting(monkeypatch):
    monkeypatch.setenv("SAVE_TRIGGER", "")
    from app.core.config import get_settings

    get_settings.cache_clear()
    try:
        handlers = ev.make_handler_factory(make_session_factory())(make_manager(), 1)
        assert len(handlers) == 1
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# _process_message — timed-only filter
# ---------------------------------------------------------------------------

async def test_non_timed_media_ignored_in_automatic_mode():
    with patch.object(ev, "classify_message", return_value=normal_info()), \
         patch.object(ev, "MediaCaptureService") as cap:
        outcome = await ev._process_message(
            message=MagicMock(),
            account_id=1,
            manager=make_manager(),
            session_factory=make_session_factory(),
            force=False,
            capture_only_timed=True,
        )
    assert outcome == "ignored"
    cap.assert_not_called()


async def test_non_timed_media_captured_when_setting_disabled():
    record = MagicMock(id=5, ttl_seconds=None, media_type=MediaType.PHOTO)
    cap_instance = MagicMock()
    cap_instance.record = AsyncMock(return_value=(record, True))
    cap_instance.mark_saved = AsyncMock()
    fwd_instance = MagicMock()
    fwd_instance.forward = AsyncMock(return_value=MagicMock(saved_message_id=99, was_resent=False))

    with patch.object(ev, "classify_message", return_value=normal_info()), \
         patch.object(ev, "_build_context", AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))), \
         patch.object(ev, "MediaCaptureService", return_value=cap_instance), \
         patch.object(ev, "SavedMessagesService", return_value=fwd_instance):
        outcome = await ev._process_message(
            message=MagicMock(),
            account_id=1,
            manager=make_manager(client=MagicMock()),
            session_factory=make_session_factory(),
            force=False,
            capture_only_timed=False,
        )
    assert outcome == "saved"


async def test_timed_media_is_saved_in_automatic_mode():
    record = MagicMock(id=5, ttl_seconds=10, media_type=MediaType.PHOTO)
    cap_instance = MagicMock()
    cap_instance.record = AsyncMock(return_value=(record, True))
    cap_instance.mark_saved = AsyncMock()
    fwd_instance = MagicMock()
    fwd_instance.forward = AsyncMock(return_value=MagicMock(saved_message_id=99, was_resent=True))

    with patch.object(ev, "classify_message", return_value=timed_info()), \
         patch.object(ev, "_build_context", AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))), \
         patch.object(ev, "MediaCaptureService", return_value=cap_instance), \
         patch.object(ev, "SavedMessagesService", return_value=fwd_instance):
        outcome = await ev._process_message(
            message=MagicMock(),
            account_id=1,
            manager=make_manager(client=MagicMock()),
            session_factory=make_session_factory(),
            force=False,
            capture_only_timed=True,
        )
    assert outcome == "saved"
    cap_instance.mark_saved.assert_awaited_once()


async def test_duplicate_is_not_reprocessed():
    record = MagicMock(id=5, status=MediaRecordStatus.SAVED)
    cap_instance = MagicMock()
    cap_instance.record = AsyncMock(return_value=(record, False))

    with patch.object(ev, "classify_message", return_value=timed_info()), \
         patch.object(ev, "_build_context", AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))), \
         patch.object(ev, "MediaCaptureService", return_value=cap_instance), \
         patch.object(ev, "SavedMessagesService") as fwd:
        outcome = await ev._process_message(
            message=MagicMock(),
            account_id=1,
            manager=make_manager(client=MagicMock()),
            session_factory=make_session_factory(),
            force=True,
            capture_only_timed=False,
        )
    assert outcome == "duplicate"
    fwd.assert_not_called()


async def test_manual_save_retries_a_previously_failed_record():
    record = MagicMock(id=5, ttl_seconds=10, media_type=MediaType.PHOTO, status=MediaRecordStatus.FAILED)
    cap_instance = MagicMock()
    cap_instance.record = AsyncMock(return_value=(record, False))
    cap_instance.mark_saved = AsyncMock()
    fwd_instance = MagicMock()
    fwd_instance.forward = AsyncMock(return_value=MagicMock(saved_message_id=99, was_resent=True))

    with patch.object(ev, "classify_message", return_value=timed_info()), \
         patch.object(ev, "_build_context", AsyncMock(return_value=(MagicMock(), MagicMock(), MagicMock()))), \
         patch.object(ev, "MediaCaptureService", return_value=cap_instance), \
         patch.object(ev, "SavedMessagesService", return_value=fwd_instance):
        outcome = await ev._process_message(
            message=MagicMock(),
            account_id=1,
            manager=make_manager(client=MagicMock()),
            session_factory=make_session_factory(),
            force=True,
            capture_only_timed=False,
        )
    assert outcome == "saved"


# ---------------------------------------------------------------------------
# Manual `.saveit` handler
# ---------------------------------------------------------------------------

def make_event(reply_msg=None, reply_to=123):
    event = MagicMock()
    event.reply_to_msg_id = reply_to if reply_msg is not None else None
    event.get_reply_message = AsyncMock(return_value=reply_msg)
    event.delete = AsyncMock()
    return event


async def test_manual_save_without_reply_notifies_owner_privately():
    client = MagicMock()
    client.send_message = AsyncMock()
    event = make_event(reply_msg=None)

    await ev._handle_manual_save(
        event=event, account_id=1, manager=make_manager(client=client),
        session_factory=make_session_factory(),
    )

    event.delete.assert_awaited_once()  # the trigger command is removed
    # notice goes to the owner's OWN Saved Messages, never into the chat
    assert client.send_message.await_args.args[0] == "me"


async def test_manual_save_on_message_without_media_notifies_owner():
    client = MagicMock()
    client.send_message = AsyncMock()
    event = make_event(reply_msg=MagicMock())

    with patch.object(ev, "classify_message", return_value=None):
        await ev._handle_manual_save(
            event=event, account_id=1, manager=make_manager(client=client),
            session_factory=make_session_factory(),
        )
    client.send_message.assert_awaited_once()


async def test_manual_save_forces_processing_of_non_timed_media():
    client = MagicMock()
    client.send_message = AsyncMock()
    replied = MagicMock()
    event = make_event(reply_msg=replied)

    with patch.object(ev, "classify_message", return_value=normal_info()), \
         patch.object(ev, "_process_message", AsyncMock(return_value="saved")) as proc:
        await ev._handle_manual_save(
            event=event, account_id=1, manager=make_manager(client=client),
            session_factory=make_session_factory(),
        )

    kwargs = proc.await_args.kwargs
    assert kwargs["message"] is replied
    assert kwargs["force"] is True
    assert kwargs["capture_only_timed"] is False
    client.send_message.assert_not_called()  # success is silent


async def test_manual_save_failure_notifies_owner():
    client = MagicMock()
    client.send_message = AsyncMock()
    event = make_event(reply_msg=MagicMock())

    with patch.object(ev, "classify_message", return_value=timed_info()), \
         patch.object(ev, "_process_message", AsyncMock(return_value="failed")):
        await ev._handle_manual_save(
            event=event, account_id=1, manager=make_manager(client=client),
            session_factory=make_session_factory(),
        )
    assert client.send_message.await_args.args[0] == "me"
    assert "Could not save" in client.send_message.await_args.args[1]


async def test_process_message_cleans_up_temp_dir(tmp_path):
    dummy_dir = tmp_path / "destrucyion_123"
    dummy_dir.mkdir()
    dummy_file = dummy_dir / "media.mp4"
    dummy_file.write_bytes(b"content")
    # Buat file thumbnail dummy agar bisa direname
    dummy_thumb = dummy_dir / "thumb.jpg"
    dummy_thumb.write_bytes(b"thumb content")

    mock_msg = MagicMock()
    mock_manager = MagicMock()
    mock_client = AsyncMock()
    mock_manager.get_client.return_value = mock_client
    mock_manager._clients = {1: MagicMock(user_id=100)}

    # download_media dipanggil dua kali: satu untuk media, satu untuk thumbnail
    mock_client.download_media = AsyncMock(side_effect=[str(dummy_thumb), str(dummy_thumb)])

    from app.services.saved_messages import ForwardResult
    mock_fwd_result = ForwardResult(
        saved_message_id=123,
        caption_used="cap",
        was_resent=True,
        file_path=str(dummy_file),
    )

    with patch("app.telegram.events.classify_message") as mock_classify, \
         patch("app.telegram.events._build_context") as mock_build_ctx, \
         patch("app.telegram.events.MediaCaptureService") as mock_cap_svc, \
         patch("app.telegram.events.SavedMessagesService") as mock_fwd_svc, \
         patch("app.telegram.admin_notify.notify_admin_of_capture", new_callable=AsyncMock) as mock_notify, \
         patch("app.services.account.AccountService") as mock_account_cls, \
         patch("app.services.subscription.SubscriptionService") as mock_sub_cls:

        mock_account_cls.return_value._get_account_for_user = AsyncMock()
        mock_sub_cls.return_value.get_subscription = AsyncMock()
        mock_classify.return_value = MagicMock(is_self_destruct=True, media_type=MagicMock(value="video"), ttl_seconds=10)
        mock_build_ctx.return_value = (MagicMock(), MagicMock(), MagicMock())

        mock_cap_instance = AsyncMock()
        mock_cap_instance.record.return_value = (MagicMock(id=1, media_type=MagicMock(value="video"), ttl_seconds=10), True)
        mock_cap_svc.return_value = mock_cap_instance

        mock_fwd_instance = AsyncMock()
        mock_fwd_instance.forward.return_value = mock_fwd_result
        mock_fwd_svc.return_value = mock_fwd_instance

        mock_session_factory = MagicMock()
        mock_session = AsyncMock()
        mock_session_factory.return_value.__aenter__.return_value = mock_session

        outcome = await ev._process_message(
            message=mock_msg,
            account_id=1,
            manager=mock_manager,
            session_factory=mock_session_factory,
            force=True,
            capture_only_timed=False,
        )

        assert outcome == "saved"
        assert mock_notify.called
        assert not dummy_dir.exists()

