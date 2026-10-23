"""
tests/unit/bot/test_capture.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
/capture command family: paywall, subscription gate, add/remove rules.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bot.handlers.capture import capture_command
from app.db.models.capture import CaptureChannel, CaptureSubscription
from app.db.models.subscription import (
    Subscription,
    SubscriptionPlan,
    SubscriptionStatus,
)
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.models.user import User
from app.services.capture_addon import CaptureAddonService
from tests.unit.bot.test_dependencies import make_update

pytestmark = pytest.mark.asyncio

TELEGRAM_ID = 123  # make_update's default user id
CHAT_ID = -1001234567890
OTHER_CHAT_ID = -1009999999999


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


async def _seed_base_sub(session: AsyncSession, user: User) -> None:
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


async def _seed_addon(session: AsyncSession, user: User) -> None:
    session.add(
        CaptureSubscription(
            user_id=user.id,
            plan=SubscriptionPlan.MONTHLY,
            starts_at=_now() - timedelta(days=1),
            expires_at=_now() + timedelta(days=29),
            status=SubscriptionStatus.ACTIVE,
        )
    )
    await session.flush()


async def _seed_registration(
    session: AsyncSession, account: TelegramAccount, chat_id: int = CHAT_ID
) -> None:
    session.add(CaptureChannel(telegram_account_id=account.id, chat_id=chat_id))
    await session.flush()


async def _run(args: list[str], session: AsyncSession, user: User):
    update = make_update(user_id=TELEGRAM_ID)
    context = MagicMock()
    context.args = args
    await capture_command.__wrapped__(update, context, session, user)
    texts = [
        str(call.args[0]) if call.args else ""
        for call in update.message.reply_text.call_args_list
    ]
    markups = [
        call.kwargs.get("reply_markup")
        for call in update.message.reply_text.call_args_list
    ]
    return texts, markups


def _all_text(texts: list[str]) -> str:
    return "\n".join(texts)


async def _registered_chat_id(session: AsyncSession, account: TelegramAccount):
    result = await session.execute(
        select(CaptureChannel).where(
            CaptureChannel.telegram_account_id == account.id
        )
    )
    row = result.scalar_one_or_none()
    return row.chat_id if row is not None else None


# ----------------------------------------------------------------------
# Bare /capture
# ----------------------------------------------------------------------

async def test_bare_capture_without_base_subscription_prompts_subscribe(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)

    texts, _ = await _run([], session, user)
    joined = _all_text(texts)
    assert "requires an active subscription first" in joined
    assert "/subscription" in joined


async def test_bare_capture_without_addon_shows_paywall(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)
    await _seed_base_sub(session, user)

    texts, markups = await _run([], session, user)
    joined = _all_text(texts)
    assert "Auto-Capture is a paid add-on" in joined

    markup = next(m for m in markups if m is not None)
    callbacks = [
        button.callback_data
        for row in markup.inline_keyboard
        for button in row
    ]
    assert callbacks == [
        "capture_plan_weekly",
        "capture_plan_monthly",
        "capture_plan_quarterly",
    ]


async def test_bare_capture_entitled_shows_registered_channel(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)
    await _seed_registration(session, account)

    texts, _ = await _run([], session, user)
    joined = _all_text(texts)
    assert "Auto-Capture is ACTIVE" in joined
    assert str(CHAT_ID) in joined


async def test_bare_capture_entitled_without_channel_shows_usage(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    texts, _ = await _run([], session, user)
    joined = _all_text(texts)
    assert "No channel is registered yet" in joined
    assert "/capture add" in joined


# ----------------------------------------------------------------------
# /capture add — argument validation
# ----------------------------------------------------------------------

async def test_add_rejects_link_argument(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    texts, _ = await _run(["add", "https://t.me/somechannel"], session, user)
    assert "Links and usernames are not accepted" in _all_text(texts)


async def test_add_rejects_username_argument(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    texts, _ = await _run(["add", "@somechannel"], session, user)
    assert "Links and usernames are not accepted" in _all_text(texts)


async def test_add_rejects_positive_user_id(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    texts, _ = await _run(["add", "123456789"], session, user)
    assert "user ID" in _all_text(texts)


async def test_add_rejects_non_numeric_argument(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    texts, _ = await _run(["add", "abc123"], session, user)
    assert "not a numeric chat ID" in _all_text(texts)


# ----------------------------------------------------------------------
# /capture add — gates and membership
# ----------------------------------------------------------------------

async def test_add_without_base_subscription_prompts_subscribe(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_addon(session, user)

    texts, _ = await _run(["add", str(CHAT_ID)], session, user)
    assert "requires an active subscription first" in _all_text(texts)
    assert await _registered_chat_id(session, account) is None


async def test_add_without_addon_shows_paywall(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_base_sub(session, user)

    texts, markups = await _run(["add", str(CHAT_ID)], session, user)
    assert "Auto-Capture is a paid add-on" in _all_text(texts)
    assert any(m is not None for m in markups)
    assert await _registered_chat_id(session, account) is None


async def test_add_success_with_membership_ok(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    with patch.object(
        CaptureAddonService,
        "verify_membership",
        new=AsyncMock(return_value=(True, "Secret Channel")),
    ):
        texts, _ = await _run(["add", str(CHAT_ID)], session, user)

    joined = _all_text(texts)
    assert "✅ Auto-Capture registered" in joined
    assert "Secret Channel" in joined
    assert await _registered_chat_id(session, account) == CHAT_ID


async def test_add_non_member_rejected(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)

    with patch.object(
        CaptureAddonService,
        "verify_membership",
        new=AsyncMock(return_value=(False, None)),
    ):
        texts, _ = await _run(["add", str(CHAT_ID)], session, user)

    assert "Could not verify membership" in _all_text(texts)
    assert await _registered_chat_id(session, account) is None


async def test_add_second_channel_rejected(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)
    await _seed_registration(session, account)

    texts, _ = await _run(["add", str(OTHER_CHAT_ID)], session, user)
    joined = _all_text(texts)
    assert "already have a registered channel" in joined
    assert str(CHAT_ID) in joined
    assert await _registered_chat_id(session, account) == CHAT_ID


async def test_add_same_channel_reports_already_registered(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_base_sub(session, user)
    await _seed_addon(session, user)
    await _seed_registration(session, account)

    texts, _ = await _run(["add", str(CHAT_ID)], session, user)
    assert "already registered" in _all_text(texts)


# ----------------------------------------------------------------------
# /capture remove
# ----------------------------------------------------------------------

async def test_remove_matching_id(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_registration(session, account)

    texts, _ = await _run(["remove", str(CHAT_ID)], session, user)
    assert "removed" in _all_text(texts)
    assert await _registered_chat_id(session, account) is None


async def test_remove_mismatched_id_shows_registered(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    account = await _seed_account(session, user)
    await _seed_registration(session, account)

    texts, _ = await _run(["remove", str(OTHER_CHAT_ID)], session, user)
    joined = _all_text(texts)
    assert "does not match your registered channel" in joined
    assert str(CHAT_ID) in joined
    assert await _registered_chat_id(session, account) == CHAT_ID


async def test_remove_without_registration(db_session: AsyncSession):
    session = db_session
    user = await _seed_user(session)
    await _seed_account(session, user)

    texts, _ = await _run(["remove", str(CHAT_ID)], session, user)
    assert "no registered Auto-Capture channel" in _all_text(texts)
