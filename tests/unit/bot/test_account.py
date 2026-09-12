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
    connect_phone,
    connect_code,
    connect_password,
    connect_cancel,
    ENTER_PHONE,
    ENTER_CODE,
    ENTER_PASSWORD,
)
from tests.unit.bot.test_dependencies import make_update

pytestmark = pytest.mark.asyncio


async def test_connect_start():
    update = make_update()
    context = MagicMock()
    context.user_data = {}
    session = MagicMock()
    user = MagicMock()
    user.id = 55
    
    result = await connect_start.__wrapped__(update, context, session, user)
    
    assert result == ENTER_PHONE
    assert context.user_data["user_id"] == 55
    update.message.reply_text.assert_called_once()
    assert "enter your phone number" in update.message.reply_text.call_args[0][0].lower()


@patch("app.bot.handlers.account.AccountService")
async def test_connect_phone_success(mock_account_service_cls):
    update = make_update()
    update.message.text = "+1234567890"
    
    context = MagicMock()
    context.user_data = {}
    session = AsyncMock()
    user = MagicMock()
    user.id = 55

    # Mock AccountService
    mock_svc = AsyncMock()
    mock_account_service_cls.return_value = mock_svc
    
    mock_acc = MagicMock()
    mock_acc.id = 99
    mock_svc.create_account.return_value = mock_acc
    
    mock_start_res = MagicMock()
    mock_start_res.phone_code_hash = "hash123"
    mock_svc.start_auth.return_value = mock_start_res
    
    result = await connect_phone.__wrapped__(update, context, session, user)
    
    assert result == ENTER_CODE
    assert context.user_data["phone"] == "+1234567890"
    assert context.user_data["account_id"] == 99
    assert context.user_data["phone_code_hash"] == "hash123"
    
    mock_svc.create_account.assert_called_once_with(user_id=55, phone_number="+1234567890")
    mock_svc.start_auth.assert_called_once_with(account_id=99, user_id=55, phone_number="+1234567890")


async def test_connect_phone_invalid_format():
    update = make_update()
    update.message.text = "1234567890" # Missing +
    
    context = MagicMock()
    session = MagicMock()
    user = MagicMock()
    
    result = await connect_phone.__wrapped__(update, context, session, user)
    
    assert result == ENTER_PHONE
    update.message.reply_text.assert_called_once()
    assert "start with '+'" in update.message.reply_text.call_args[0][0]


@patch("app.bot.handlers.account.AccountService")
async def test_connect_code_success(mock_account_service_cls):
    update = make_update()
    update.message.text = "55555"
    
    context = MagicMock()
    context.user_data = {
        "phone": "+1234567890",
        "account_id": 99,
        "phone_code_hash": "hash123"
    }
    session = AsyncMock()
    user = MagicMock()
    user.id = 55

    mock_svc = AsyncMock()
    mock_account_service_cls.return_value = mock_svc
    
    result = await connect_code.__wrapped__(update, context, session, user)
    
    assert result == ConversationHandler.END
    mock_svc.verify_code.assert_called_once_with(
        account_id=99, user_id=55, phone_number="+1234567890", code="55555", phone_code_hash="hash123"
    )
    # Context should be cleared on success
    assert not context.user_data


@patch("app.bot.handlers.account.AccountService")
async def test_connect_code_needs_password(mock_account_service_cls):
    update = make_update()
    update.message.text = "55555"
    
    context = MagicMock()
    context.user_data = {
        "phone": "+1234567890",
        "account_id": 99,
        "phone_code_hash": "hash123"
    }
    session = AsyncMock()
    user = MagicMock()
    
    from telethon.errors import SessionPasswordNeededError
    
    mock_svc = AsyncMock()
    mock_svc.verify_code.side_effect = SessionPasswordNeededError(request=None)
    mock_account_service_cls.return_value = mock_svc
    
    result = await connect_code.__wrapped__(update, context, session, user)
    
    assert result == ENTER_PASSWORD
    assert context.user_data["code"] == "55555"
