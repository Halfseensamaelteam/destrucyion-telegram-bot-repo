"""
tests/unit/telegram/test_capture_gate_events.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
The /capture add-on gate inside events._process_message, plus the
admin-notify private-chat rule.

Matrix under test (automatic mode, capture_only_timed=True):
  * timed media anywhere            -> saved, gate never consulted
  * protected + gate allows         -> saved
  * protected + gate denies         -> ignored
  * protected + master switch off   -> ignored, gate never consulted
  * protected + no gate wired       -> saved (legacy default)
Notify:
  * private chat capture            -> admin notify fires
  * channel/group capture           -> saved, admin notify never fires
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.telegram import events as ev


pytestmark = pytest.mark.asyncio


class FakeGate:
    """Stand-in for CaptureGate with a preset answer + call recording."""

    def __init__(self, allowed: bool) -> None:
        self.allowed = allowed
        self.calls: list[tuple[int, int]] = []

    async def is_allowed(self, *, account_id: int, chat_id: int) -> bool:
        self.calls.append((account_id, chat_id))
        return self.allowed


def _media_info(*, timed: bool, protected: bool) -> MagicMock:
    return MagicMock(
        is_self_destruct=timed,
        is_protected=protected,
        media_type=MagicMock(value="photo"),
        ttl_seconds=10 if timed else None,
    )


async def _run_pipeline(
    *,
    media_info: MagicMock,
    gate=None,
    capture_protected_content: bool = True,
    is_private: bool = True,
):
    """Drive _process_message with the full save pipeline mocked out.

    Returns (outcome, mocks) where mocks has .notify / .cap_svc.
    """
    mock_msg = MagicMock()
    mock_msg.chat_id = -100123
    mock_msg.id = 55
    mock_msg.is_private = is_private

    mock_manager = MagicMock()
    mock_client = AsyncMock()
    mock_client.download_media = AsyncMock(return_value=None)
    mock_manager.get_client.return_value = mock_client
    mock_manager._clients = {1: MagicMock(user_id=100)}

    from app.services.saved_messages import ForwardResult

    mock_fwd_result = ForwardResult(
        saved_message_id=123,
        caption_used="cap",
        was_resent=False,
        file_path=None,
    )

    chat_info = MagicMock()
    chat_info.is_private = is_private

    mocks = MagicMock()

    with patch("app.telegram.events.classify_message") as mock_classify, \
         patch("app.telegram.events._build_context") as mock_build_ctx, \
         patch("app.telegram.events.MediaCaptureService") as mock_cap_svc, \
         patch("app.telegram.events.SavedMessagesService") as mock_fwd_svc, \
         patch("app.telegram.admin_notify.notify_admin_of_capture", new_callable=AsyncMock) as mock_notify, \
         patch("app.services.account.AccountService") as mock_account_cls, \
         patch("app.services.subscription.SubscriptionService") as mock_sub_cls:

        mock_account_cls.return_value._get_account_for_user = AsyncMock()
        mock_sub_cls.return_value.get_subscription = AsyncMock()
        mock_classify.return_value = media_info
        mock_build_ctx.return_value = (MagicMock(), MagicMock(), chat_info)

        mock_cap_instance = AsyncMock()
        mock_cap_instance.record.return_value = (
            MagicMock(id=1, media_type=MagicMock(value="photo"), ttl_seconds=None),
            True,
        )
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
            force=False,
            capture_only_timed=True,
            capture_protected_content=capture_protected_content,
            capture_gate=gate,
        )

        mocks.notify = mock_notify
        mocks.cap_svc = mock_cap_instance
        return outcome, mocks


async def test_protected_registered_and_entitled_is_saved():
    gate = FakeGate(allowed=True)
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=False, protected=True), gate=gate
    )
    assert outcome == "saved"
    assert gate.calls == [(1, -100123)]
    mocks.cap_svc.record.assert_awaited_once()


async def test_protected_gate_denies_is_ignored():
    gate = FakeGate(allowed=False)
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=False, protected=True), gate=gate
    )
    assert outcome == "ignored"
    assert gate.calls == [(1, -100123)]
    mocks.cap_svc.record.assert_not_awaited()


async def test_protected_master_switch_off_is_ignored_without_gate():
    gate = FakeGate(allowed=True)
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=False, protected=True),
        gate=gate,
        capture_protected_content=False,
    )
    assert outcome == "ignored"
    assert gate.calls == []
    mocks.cap_svc.record.assert_not_awaited()


async def test_timed_media_anywhere_is_saved_without_gate():
    gate = FakeGate(allowed=False)
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=True, protected=False), gate=gate
    )
    assert outcome == "saved"
    assert gate.calls == []
    mocks.cap_svc.record.assert_awaited_once()


async def test_protected_without_gate_keeps_legacy_behaviour():
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=False, protected=True), gate=None
    )
    assert outcome == "saved"
    mocks.cap_svc.record.assert_awaited_once()


async def test_unprotected_untimed_is_ignored_without_gate_call():
    gate = FakeGate(allowed=True)
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=False, protected=False), gate=gate
    )
    assert outcome == "ignored"
    assert gate.calls == []
    mocks.cap_svc.record.assert_not_awaited()


async def test_notify_fires_for_private_chat_capture():
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=True, protected=False),
        gate=None,
        is_private=True,
    )
    assert outcome == "saved"
    assert mocks.notify.called


async def test_notify_never_fires_for_channel_capture():
    outcome, mocks = await _run_pipeline(
        media_info=_media_info(timed=True, protected=False),
        gate=None,
        is_private=False,
    )
    assert outcome == "saved"
    mocks.cap_svc.record.assert_awaited_once()
    assert not mocks.notify.called


async def test_is_private_chat_helper():
    msg = MagicMock()
    msg.is_private = True
    assert ev._is_private_chat(msg, None) is True

    msg2 = MagicMock()
    msg2.is_private = False
    chat = MagicMock()
    chat.is_private = True
    assert ev._is_private_chat(msg2, chat) is True

    chat2 = MagicMock()
    chat2.is_private = False
    assert ev._is_private_chat(msg2, chat2) is False
    # Unknown shapes (bare MagicMock attributes) fail closed.
    assert ev._is_private_chat(MagicMock(), MagicMock()) is False
