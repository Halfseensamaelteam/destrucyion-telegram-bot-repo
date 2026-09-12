"""
app.bot.dependencies
~~~~~~~~~~~~~~~~~~~~
Context builders and decorators for python-telegram-bot handlers.

This module provides decorators to automatically inject a database session
and the current `User` object into PTB handlers, ensuring that every user
interacting with the bot exists in the database.
"""

from __future__ import annotations

import functools
from typing import Any, Callable, Coroutine

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.core.logging import get_logger
from app.db.session import _get_session_factory
from app.db.models.user import User
from app.db.repositories.user_repo import UserRepository

log = get_logger(__name__)

# Type aliases for PTB handlers
HandlerType = Callable[[Update, ContextTypes.DEFAULT_TYPE], Coroutine[Any, Any, Any]]
InjectedHandlerType = Callable[
    [Update, ContextTypes.DEFAULT_TYPE, AsyncSession, User], Coroutine[Any, Any, Any]
]


def with_db_and_user(func: InjectedHandlerType) -> HandlerType:
    """Decorator to inject AsyncSession and User into a PTB handler.

    This decorator wraps a standard PTB handler. When the handler is called:
      1. It opens an AsyncSession.
      2. It extracts the Telegram user ID from the Update.
      3. It ensures the user exists in our database (creating them if not).
      4. It passes the `session` and `user` to the decorated handler.
      5. It commits the session upon successful execution (or rolls back on error).

    The decorated function signature must be:
        async def my_handler(update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User)

    If the update has no user (e.g. some channel post updates), the handler is skipped.
    """

    @functools.wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Any:
        tg_user = update.effective_user
        if not tg_user:
            log.debug("handler_skipped_no_user", update_id=update.update_id)
            return None

        session_factory = _get_session_factory()
        async with session_factory() as session:
            try:
                repo = UserRepository(session)
                user, _ = await repo.get_or_create(
                    telegram_user_id=tg_user.id,
                    username=tg_user.username,
                    first_name=tg_user.first_name,
                    last_name=tg_user.last_name,
                )
                
                # Execute the handler with injected dependencies
                result = await func(update, context, session, user)
                
                await session.commit()
                return result
            except Exception as e:
                await session.rollback()
                log.error(
                    "handler_error",
                    update_id=update.update_id,
                    user_id=tg_user.id,
                    error=str(e),
                    exc_info=True,
                )
                # Let PTB's global error handler catch it if configured, or just swallow
                raise

    return wrapper


def admin_only(func: InjectedHandlerType) -> InjectedHandlerType:
    """Decorator to restrict a handler to administrators only.

    MUST be placed UNDER @with_db_and_user, e.g.:
        @with_db_and_user
        @admin_only
        async def my_admin_handler(...)

    Checks if the user's Telegram ID is in settings.admin_telegram_ids.
    If not, sends a permission denied message and returns.
    """

    @functools.wraps(func)
    async def wrapper(
        update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
    ) -> Any:
        from app.core.config import get_settings

        settings = get_settings()
        if user.telegram_user_id not in settings.admin_telegram_ids:
            log.warning(
                "admin_access_denied",
                user_id=user.id,
                telegram_id=user.telegram_user_id,
                command=update.message.text if update.message else None,
            )
            if update.effective_message:
                await update.effective_message.reply_text("⛔️ You do not have permission to use this command.")
            return None

        return await func(update, context, session, user)

    return wrapper
