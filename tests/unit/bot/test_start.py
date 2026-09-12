"""
tests/unit/bot/test_start.py
Unit tests for app.bot.handlers.start
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from telegram import Update
from telegram.ext import ContextTypes

from app.bot.handlers.start import start_command, help_command, status_command

# Import our helper from test_dependencies
from tests.unit.bot.test_dependencies import make_update

pytestmark = pytest.mark.asyncio


async def test_start_command():
    update = make_update(first_name="Alice")
    context = MagicMock()
    session = MagicMock()
    user = MagicMock()
    user.first_name = "Alice"
    
    # We call the unwrapped function because the decorator is tested in test_dependencies
    # and requires DB patching which is overkill for testing just the text output.
    from app.bot.handlers.start import start_command
    
    # The decorator is wrapped. We can access the original function via __wrapped__
    await start_command.__wrapped__(update, context, session, user)
    
    update.message.reply_text.assert_called_once()
    args = update.message.reply_text.call_args[0][0]
    assert "Hello Alice!" in args
    assert "/connect" in args


async def test_help_command_normal_user():
    update = make_update()
    context = MagicMock()
    session = MagicMock()
    user = MagicMock()
    user.telegram_user_id = 111 # Not admin
    
    from app.core.config import get_settings
    settings = get_settings()
    settings.admin_telegram_ids = [999]
    
    from app.bot.handlers.start import help_command
    await help_command.__wrapped__(update, context, session, user)
    
    update.message.reply_text.assert_called_once()
    args = update.message.reply_text.call_args[0][0]
    assert "/start" in args
    assert "Admin Commands" not in args


async def test_help_command_admin_user():
    update = make_update()
    context = MagicMock()
    session = MagicMock()
    user = MagicMock()
    user.telegram_user_id = 999 # Admin
    
    from app.core.config import get_settings
    settings = get_settings()
    settings.admin_telegram_ids = [999]
    
    from app.bot.handlers.start import help_command
    await help_command.__wrapped__(update, context, session, user)
    
    update.message.reply_text.assert_called_once()
    args = update.message.reply_text.call_args[0][0]
    assert "Admin Commands" in args
    assert "/admin users" in args
