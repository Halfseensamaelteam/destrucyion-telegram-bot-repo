"""
tests/unit/bot/test_account.py
Unit tests for app.bot.handlers.account — QR login flow (CLAUDE.md §12).
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram.ext import ApplicationHandlerStop, ConversationHandler

from tests.unit.bot.test_dependencies import make_update
from app.bot.handlers.account import (
    ENTER_API_HASH,
    ENTER_API_ID,
    _pending_2fa_futures,
    _run_qr_login_task,
    connect_api_hash,
    connect_api_id,
    connect_start,
    maybe_capture_2fa_password,
)
from app.services.account import AccountAlreadyExistsError

pytestmark = pytest.mark.asyncio


def _make_context(text: str | None = None):
    context = MagicMock()
    context.user_data = {}
    context.bot = AsyncMock()
    return context


# ---------------------------------------------------------------------------
# /connect conversation — collects api_id/api_hash only
# ---------------------------------------------------------------------------

async def test_connect_start_asks_for_api_id():
    update = make_update(first_name="Alice")
    context = _make_context()
    session = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = None
    session.execute = AsyncMock(return_value=result_mock)
    user = MagicMock(id=55)

    result = await connect_start.__wrapped__(update, context, session, user)

    assert result == ENTER_API_ID
    update.message.reply_text.assert_called_once()
    assert context.user_data["user_id"] == 55


async def test_connect_start_rejects_if_already_connected():
    update = make_update(first_name="Alice")
    context = _make_context()
    session = MagicMock()
    existing = MagicMock()
    existing.status.value = "active"
    result_mock = MagicMock()
    result_mock.scalar_one_or_none.return_value = existing
    session.execute = AsyncMock(return_value=result_mock)
    user = MagicMock(id=55)

    result = await connect_start.__wrapped__(update, context, session, user)

    assert result == ConversationHandler.END
    update.message.reply_text.assert_called_once()
    assert "already have" in update.message.reply_text.call_args.args[0]


async def test_connect_api_id_rejects_non_numeric():
    update = make_update()
    update.message.text = "not-a-number"
    context = _make_context()
    session = MagicMock()
    user = MagicMock(id=55)

    result = await connect_api_id.__wrapped__(update, context, session, user)

    assert result == ENTER_API_ID  # stays on the same state
    assert "api_id" not in context.user_data


async def test_connect_api_id_accepts_numeric():
    update = make_update()
    update.message.text = "12345678"
    context = _make_context()
    session = MagicMock()
    user = MagicMock(id=55)

    result = await connect_api_id.__wrapped__(update, context, session, user)

    assert result == ENTER_API_HASH
    assert context.user_data["api_id"] == 12345678


async def test_connect_api_hash_creates_account_and_ends_conversation():
    update = make_update()
    update.message.text = "abcd1234efgh5678ijkl9012mnop3456"
    update.message.delete = AsyncMock()
    context = _make_context()
    context.user_data["api_id"] = 12345678
    session = MagicMock()
    session.commit = AsyncMock()
    user = MagicMock(id=55)

    mock_account = MagicMock(id=101)

    with patch("app.bot.handlers.account.AccountService") as MockSvc, \
         patch("asyncio.create_task") as mock_create_task:
        MockSvc.return_value.create_or_reset_account = AsyncMock(return_value=mock_account)

        result = await connect_api_hash.__wrapped__(update, context, session, user)

    # asyncio.create_task was mocked, so the coroutine passed to it was
    # never actually awaited/scheduled — close it explicitly to avoid a
    # "coroutine was never awaited" ResourceWarning.
    scheduled_coro = mock_create_task.call_args.args[0]
    scheduled_coro.close()

    assert result == ConversationHandler.END
    # Sensitive api_hash message must be deleted from chat history
    update.message.delete.assert_awaited_once()
    # A background task must have been scheduled for the actual QR login
    mock_create_task.assert_called_once()
    # Conversation state must be cleared
    assert context.user_data == {}


async def test_connect_api_hash_rejects_second_account():
    update = make_update()
    update.message.text = "abcd1234efgh5678ijkl9012mnop3456"
    update.message.delete = AsyncMock()
    update.effective_chat.send_message = AsyncMock()
    context = _make_context()
    context.user_data["api_id"] = 12345678
    session = MagicMock()
    session.rollback = AsyncMock()
    user = MagicMock(id=55)

    with patch("app.bot.handlers.account.AccountService") as MockSvc, \
         patch("asyncio.create_task") as mock_create_task:
        MockSvc.return_value.create_or_reset_account = AsyncMock(
            side_effect=AccountAlreadyExistsError("already have one")
        )

        result = await connect_api_hash.__wrapped__(update, context, session, user)

    assert result == ConversationHandler.END
    mock_create_task.assert_not_called()
    update.effective_chat.send_message.assert_called_once()


# ---------------------------------------------------------------------------
# 2FA password capture (outside the ConversationHandler)
# ---------------------------------------------------------------------------

async def test_maybe_capture_2fa_password_resolves_pending_future():
    update = make_update()
    update.message.text = "my-secret-password"
    update.message.delete = AsyncMock()
    context = MagicMock()

    chat_id = update.effective_chat.id
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    _pending_2fa_futures[chat_id] = future

    try:
        with pytest.raises(ApplicationHandlerStop):
            await maybe_capture_2fa_password(update, context)

        assert future.done()
        assert future.result() == "my-secret-password"
        update.message.delete.assert_awaited_once()
    finally:
        _pending_2fa_futures.pop(chat_id, None)


async def test_maybe_capture_2fa_password_noop_without_pending_future():
    update = make_update()
    update.message.text = "just a normal message"
    update.message.delete = AsyncMock()
    context = MagicMock()

    # No entry in _pending_2fa_futures for this chat — must do nothing and
    # must NOT raise ApplicationHandlerStop (so other handlers still run).
    result = await maybe_capture_2fa_password(update, context)

    assert result is None
    update.message.delete.assert_not_awaited()


# ---------------------------------------------------------------------------
# _run_qr_login_task — the detached background task
# ---------------------------------------------------------------------------

async def test_run_qr_login_task_success_sends_confirmation():
    bot = AsyncMock()

    with patch("app.bot.handlers.account._get_session_factory") as mock_factory, \
         patch("app.bot.handlers.account.AccountService") as MockSvc:
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = False
        mock_factory.return_value = MagicMock(return_value=mock_session)
        MockSvc.return_value.perform_qr_login = AsyncMock(return_value=MagicMock())

        await _run_qr_login_task(bot=bot, chat_id=123, account_id=1, user_id=55)

    bot.send_message.assert_any_call(123, "✅ Success! Your account is now connected and ACTIVE.")


async def test_run_qr_login_task_failure_sends_error_and_cleans_up_future():
    from app.services.account import AuthTimeoutError

    bot = AsyncMock()
    chat_id = 456
    _pending_2fa_futures[chat_id] = asyncio.get_event_loop().create_future()

    with patch("app.bot.handlers.account._get_session_factory") as mock_factory, \
         patch("app.bot.handlers.account.AccountService") as MockSvc:
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_session.__aexit__.return_value = False
        mock_factory.return_value = MagicMock(return_value=mock_session)
        MockSvc.return_value.perform_qr_login = AsyncMock(
            side_effect=AuthTimeoutError("QR code was not scanned in time.")
        )

        await _run_qr_login_task(bot=bot, chat_id=chat_id, account_id=1, user_id=55)

    assert chat_id not in _pending_2fa_futures
    sent_text = bot.send_message.call_args_list[-1].args[1]
    assert "QR code was not scanned in time" in sent_text
