"""
tests/unit/bot/test_start.py
Unit tests for app.bot.handlers.start
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

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


async def test_status_lifetime_subscription_does_not_crash():
    """Regression: /status used to crash with AttributeError on lifetime
    plans (expires_at=None), same root cause as /subscription and
    /admin grant."""
    from app.bot.handlers.start import status_command
    from app.db.models.subscription import SubscriptionPlan

    raw_status_command = status_command.__wrapped__
    update = make_update()
    session = MagicMock()
    sub = MagicMock()
    sub.plan = SubscriptionPlan.LIFETIME
    sub.expires_at = None

    with patch("app.bot.handlers.start.TelegramAccountRepository") as MockAcctRepo, \
         patch("app.bot.handlers.start.SubscriptionRepository") as MockSubRepo:
        MockAcctRepo.return_value.list_by_user = AsyncMock(return_value=[])
        MockSubRepo.return_value.get_by_user_id = AsyncMock(return_value=sub)

        await raw_status_command(update, MagicMock(), session, MagicMock(id=1))

    text = update.message.reply_text.call_args.args[0]
    assert "Never" in text
