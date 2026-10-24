"""
tests/unit/bot/test_save_gate.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
/save one-active-request gate: a second link is refused while an
earlier request for the same account is still PENDING or PROCESSING,
and creates no row and no "Queued" message.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from telegram.ext import ContextTypes

from app.bot.handlers.restricted import save_link_command
from app.db.models.link_request import LinkRequest, LinkRequestStatus
from app.db.models.subscription import (
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.user import User
from app.db.repositories.link_request_repo import LinkRequestRepository
from tests.unit.bot.test_dependencies import make_update

pytestmark = pytest.mark.asyncio

TELEGRAM_ID = 123  # make_update's default user id


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _seed(session: AsyncSession):
    user = User(telegram_user_id=TELEGRAM_ID, username="testuser", first_name="Test")
    session.add(user)
    await session.flush()
    await session.refresh(user)
    account = TelegramAccount(
        user_id=user.id,
        phone_masked="+62****1234",
        status=TelegramAccountStatus.ACTIVE,
    )
    session.add(account)
    await session.flush()
    await session.refresh(account)
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
    return user, account


def _update_with_text(text: str):
    update = make_update()
    update.message.text = text
    update.message.reply_text = AsyncMock(
        return_value=SimpleNamespace(chat_id=555, message_id=777)
    )
    return update


async def _row_count(session: AsyncSession) -> int:
    result = await session.execute(select(LinkRequest))
    return len(result.scalars().all())


async def _call_save(db_session, user, text):
    update = _update_with_text(text)
    await save_link_command.__wrapped__(
        update, MagicMock(spec=ContextTypes.DEFAULT_TYPE), db_session, user
    )
    return update


async def test_pending_row_blocks_new_save(db_session: AsyncSession):
    user, account = await _seed(db_session)
    repo = LinkRequestRepository(db_session)
    await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/chan/1",
    )
    await db_session.flush()

    update = await _call_save(db_session, user, "/save https://t.me/chan/2")
    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("still have a save in progress" in t for t in texts)
    assert not any("Queued" in t for t in texts)
    assert await _row_count(db_session) == 1


async def test_processing_row_blocks_new_save(db_session: AsyncSession):
    user, account = await _seed(db_session)
    repo = LinkRequestRepository(db_session)
    row = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/chan/1",
    )
    await repo.mark(row, status=LinkRequestStatus.PROCESSING)
    await db_session.flush()

    update = await _call_save(db_session, user, "/save https://t.me/chan/2")
    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert any("still have a save in progress" in t for t in texts)
    assert await _row_count(db_session) == 1


async def test_no_active_row_queues_normally(db_session: AsyncSession):
    user, account = await _seed(db_session)
    update = await _call_save(db_session, user, "/save https://t.me/chan/1")
    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert texts == ["⏳ Queued…"]
    assert await _row_count(db_session) == 1


async def test_done_row_does_not_block(db_session: AsyncSession):
    user, account = await _seed(db_session)
    repo = LinkRequestRepository(db_session)
    row = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/chan/1",
    )
    await repo.mark(row, status=LinkRequestStatus.DONE, result_count=1)
    await db_session.flush()

    update = await _call_save(db_session, user, "/save https://t.me/chan/2")
    texts = [c.args[0] for c in update.message.reply_text.await_args_list]
    assert texts == ["⏳ Queued…"]
    assert await _row_count(db_session) == 2


async def test_has_active_repo_query(db_session: AsyncSession):
    user, account = await _seed(db_session)
    repo = LinkRequestRepository(db_session)
    assert await repo.has_active(account.id) is False
    row = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/chan/1",
    )
    await db_session.flush()
    assert await repo.has_active(account.id) is True
    await repo.mark(row, status=LinkRequestStatus.FAILED, error="x")
    await db_session.flush()
    assert await repo.has_active(account.id) is False
