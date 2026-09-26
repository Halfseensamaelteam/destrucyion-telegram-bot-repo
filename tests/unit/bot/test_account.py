"""
tests/unit/bot/test_account.py
Unit tests for app.bot.handlers.account
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from telegram import Update
from telegram.ext import ConversationHandler

from app.bot.handlers.account import (
    accounts_command,
    connect_start,
    connect_api_id,
    connect_api_hash,
    connect_cancel,
    ENTER_API_ID,
    ENTER_API_HASH,
)
from tests.unit.bot.test_dependencies import make_update

pytestmark = pytest.mark.asyncio


async def test_connect_start():
    update = make_update()
    context = MagicMock()
    context.user_data = {}
    session = AsyncMock()
    user = MagicMock()
    user.id = 55
    
    # Mock repo to return empty list (no existing accounts)
    from app.db.repositories.telegram_account_repo import TelegramAccountRepository
    mock_repo = AsyncMock()
    mock_repo.list_by_user.return_value = []
    
    with patch("app.bot.handlers.account.TelegramAccountRepository", return_value=mock_repo):
        result = await connect_start.__wrapped__(update, context, session, user)
    
    assert result == ENTER_API_ID
    assert context.user_data["user_id"] == 55
    update.message.reply_text.assert_called_once()
    assert "api id" in update.message.reply_text.call_args[0][0].lower()


async def test_connect_api_id_valid():
    update = make_update()
    update.message.text = "12345"
    
    context = MagicMock()
    context.user_data = {}
    session = MagicMock()
    user = MagicMock()
    
    result = await connect_api_id.__wrapped__(update, context, session, user)
    
    assert result == ENTER_API_HASH
    assert context.user_data["api_id"] == 12345
    update.message.reply_text.assert_called_once()


async def test_connect_api_id_invalid():
    update = make_update()
    update.message.text = "not_a_number"
    
    context = MagicMock()
    session = MagicMock()
    user = MagicMock()
    
    result = await connect_api_id.__wrapped__(update, context, session, user)
    
    assert result == ENTER_API_ID
    update.message.reply_text.assert_called_once()
    assert "must be a number" in update.message.reply_text.call_args[0][0].lower()


@patch("app.bot.handlers.account.AccountService")
async def test_connect_api_hash_success(mock_account_service_cls):
    update = make_update()
    update.message.text = "abcdef123456"
    
    context = MagicMock()
    context.user_data = {"api_id": 12345}
    session = AsyncMock()
    user = MagicMock()
    user.id = 55

    mock_svc = AsyncMock()
    mock_account_service_cls.return_value = mock_svc
    
    mock_acc = MagicMock()
    mock_acc.id = 99
    mock_svc.create_account.return_value = mock_acc
    
    mock_qr_result = MagicMock()
    mock_qr_result.qr_code_bytes = b"fake_qr_bytes"
    mock_qr_result.login_obj = MagicMock()
    mock_svc.start_qr_auth.return_value = mock_qr_result
    
    result = await connect_api_hash.__wrapped__(update, context, session, user)
    
    assert result == ConversationHandler.END
    assert context.user_data["account_id"] == 99
    assert context.user_data["api_id"] == 12345
    assert context.user_data["api_hash"] == "abcdef123456"
    
    mock_svc.create_account.assert_called_once_with(user_id=55, phone_number="QR_AUTH")
    mock_svc.start_qr_auth.assert_called_once_with(
        account_id=99, user_id=55, api_id=12345, api_hash="abcdef123456"
    )
