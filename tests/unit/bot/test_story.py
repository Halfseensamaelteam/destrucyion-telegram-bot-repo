"""
tests/unit/bot/test_story.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
/story command: link validation, account/subscription gates, queueing.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram.ext import ContextTypes

from app.bot.handlers.story import story_command
from app.db.models.link_request import LinkRequest
from app.db.models.subscription import (
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.user import User
from tests.unit.bot.test_dependencies import make_update

pytestmark = pytest.mark.asyncio

TELEGRAM_ID = 123  # make_update's default user id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed_user(session: AsyncSession) -> User:
    user = User(telegram_user_id=TELEGRAM_ID, username="testuser", first_name="Test")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    return user


async def _seed_account(session: AsyncSession, user: User) -> TelegramAccount:
    account = TelegramAccount(
        user_id=user.id,
        phone_masked="+62****1234",
        status=TelegramAccountStatus.ACTIVE,
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
    return account


async def _seed_sub(session: AsyncSession, user: User) -> None:
    session.add(
        Subscription(
            user_id=user.id,
            plan=SubscriptionPlan.MONTHLY,
            starts_at=_now() - timedelta(days=1),
            expires_at=_now() + timedelta(days=29),
            status=SubscriptionStatus.ACTIVE,
        )
    )
    await session.flush()


def _update_with_text(text: str):
    update = make_update()
    update.message.text = text
    # The "Queued" status message's ids ride on the queue row.
    update.message.reply_text = AsyncMock(
        return_value=SimpleNamespace(chat_id=555, message_id=777)
    )
    return update


async def _row_count(session: AsyncSession) -> int:
    result = await session.execute(select(LinkRequest))
    return len(result.scalars().all())


async def test_invalid_link_gets_hint(db_session: AsyncSession):
    user = await _seed_user(db_session)
    update = _update_with_text("/story https://t.me/chan/42")
    await story_command.__wrapped__(
        update, MagicMock(spec=ContextTypes.DEFAULT_TYPE), db_session, user
    )
    text = update.message.reply_text.await_args.args[0]
    assert "story link" in text
    assert await _row_count(db_session) == 0


async def test_no_account_rejected(db_session: AsyncSession):
    user = await _seed_user(db_session)
    update = _update_with_text("/story https://t.me/someone/s/7")
    await story_command.__wrapped__(
        update, MagicMock(spec=ContextTypes.DEFAULT_TYPE), db_session, user
    )
    text = update.message.reply_text.await_args.args[0]
    assert "no connected Telegram account" in text
    assert await _row_count(db_session) == 0


async def test_no_subscription_rejected(db_session: AsyncSession):
    user = await _seed_user(db_session)
    await _seed_account(db_session, user)
    update = _update_with_text("/story https://t.me/someone/s/7")
    await story_command.__wrapped__(
        update, MagicMock(spec=ContextTypes.DEFAULT_TYPE), db_session, user
    )
    text = update.message.reply_text.await_args.args[0]
    assert "active subscription" in text
    assert await _row_count(db_session) == 0


async def test_single_story_queues_row(db_session: AsyncSession):
    user = await _seed_user(db_session)
    account = await _seed_account(db_session, user)
    await _seed_sub(db_session, user)
    update = _update_with_text("/story https://t.me/someone/s/7")
    await story_command.__wrapped__(
        update, MagicMock(spec=ContextTypes.DEFAULT_TYPE), db_session, user
    )
    rows = (await db_session.execute(select(LinkRequest))).scalars().all()
    assert len(rows) == 1
    assert rows[0].telegram_account_id == account.id
    assert rows[0].raw_link == "https://t.me/someone/s/7"
    assert rows[0].notify_chat_id == 555
    assert rows[0].notify_message_id == 777
    assert update.message.reply_text.await_args.args[0] == "⏳ Queued…"


async def test_bare_username_queues_row(db_session: AsyncSession):
    user = await _seed_user(db_session)
    await _seed_account(db_session, user)
    await _seed_sub(db_session, user)
    update = _update_with_text("/story https://t.me/someone")
    await story_command.__wrapped__(
        update, MagicMock(spec=ContextTypes.DEFAULT_TYPE), db_session, user
    )
    rows = (await db_session.execute(select(LinkRequest))).scalars().all()
    assert len(rows) == 1
    assert rows[0].raw_link == "https://t.me/someone"
