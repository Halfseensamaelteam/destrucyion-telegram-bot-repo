"""
tests/unit/bot/test_dependencies.py
Unit tests for app.bot.dependencies
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update, User as TelegramUser
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user, admin_only
from app.db.models.user import User

pytestmark = pytest.mark.asyncio


def make_update(user_id=123, username="testuser", first_name="Test") -> MagicMock:
    update = MagicMock(spec=Update)
    update.update_id = 1
    
    tg_user = MagicMock(spec=TelegramUser)
    tg_user.id = user_id
    tg_user.username = username
    tg_user.first_name = first_name
    tg_user.last_name = None
    
    update.effective_user = tg_user
    
    msg = MagicMock()
    msg.reply_text = AsyncMock()
    msg.text = "/dummy"
    update.message = msg
    update.effective_message = msg
    
    return update


async def test_with_db_and_user_injects_args_and_creates_user(db_session: AsyncSession):
    """Verifies that the decorator creates a User in the DB and injects it."""
    
    @with_db_and_user
    async def dummy_handler(update, context, session, user):
        assert isinstance(session, AsyncSession)
        assert isinstance(user, User)
        assert user.telegram_user_id == 123
        assert user.username == "testuser"
        return "success"

    update = make_update()
    context = MagicMock(spec=ContextTypes.DEFAULT_TYPE)
    
    # We must patch AsyncSessionLocal inside dependencies.py
    # to yield our test db_session.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session
        
    import app.bot.dependencies as deps
    original_factory = deps._get_session_factory
    deps._get_session_factory = lambda: mock_factory
    
    try:
        result = await dummy_handler(update, context)
        assert result == "success"
        
        # Verify user was saved
        from app.db.repositories.user_repo import UserRepository
        repo = UserRepository(db_session)
        db_user = await repo.get_by_telegram_id(123)
        assert db_user is not None
        assert db_user.first_name == "Test"
    finally:
        deps._get_session_factory = original_factory


async def test_with_db_and_user_skips_if_no_user():
    """If update has no user (e.g. some channel updates), handler is skipped."""
    @with_db_and_user
    async def dummy_handler(*args):
        pytest.fail("Handler should not be called")

    update = make_update()
    update.effective_user = None
    context = MagicMock()
    
    result = await dummy_handler(update, context)
    assert result is None


async def test_admin_only_allows_admin(monkeypatch):
    """If user is in admin list, handler is executed."""
    
    @admin_only
    async def dummy_handler(update, context, session, user):
        return "admin_success"

    update = make_update()
    context = MagicMock()
    session = MagicMock()
    user = MagicMock(spec=User)
    user.telegram_user_id = 999
    
    from app.core.config import get_settings
    settings = get_settings()
    settings.admin_telegram_ids = [999]

    result = await dummy_handler(update, context, session, user)
    assert result == "admin_success"
    update.effective_message.reply_text.assert_not_called()


async def test_admin_only_blocks_non_admin():
    """If user is not in admin list, handler is blocked and replied to."""
    
    @admin_only
    async def dummy_handler(update, context, session, user):
        pytest.fail("Should not execute")

    update = make_update()
    context = MagicMock()
    session = MagicMock()
    user = MagicMock(spec=User)
    user.telegram_user_id = 111 # Not an admin
    
    from app.core.config import get_settings
    settings = get_settings()
    settings.admin_telegram_ids = [999]

    result = await dummy_handler(update, context, session, user)
    assert result is None
    update.effective_message.reply_text.assert_called_once_with(
        "⛔️ You do not have permission to use this command."
    )
