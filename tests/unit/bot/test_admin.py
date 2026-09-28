"""
tests/unit/bot/test_admin.py
Tests for /admin grant and /admin revoke — the only way to give a customer
a subscription through the bot (there was previously no test coverage for
admin.py at all).
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.bot.test_dependencies import make_update
from app.bot.handlers.admin import admin_command
from app.db.models.subscription import SubscriptionPlan
from app.services.subscription import SubscriptionError

pytestmark = pytest.mark.asyncio

# Skip BOTH @with_db_and_user and @admin_only — this file tests business
# logic, not the admin gate itself (that's covered by dependencies tests).
raw_admin_command = admin_command.__wrapped__.__wrapped__


def _make_context(args):
    context = MagicMock()
    context.args = args
    return context


async def test_grant_requires_two_arguments():
    update = make_update()
    await raw_admin_command(update, _make_context(["grant", "12345"]), MagicMock(), MagicMock())
    assert "Usage" in update.message.reply_text.call_args.args[0]


async def test_grant_rejects_unknown_plan():
    update = make_update()
    session = MagicMock()

    with patch("app.bot.handlers.admin.UserRepository") as MockUserRepo:
        MockUserRepo.return_value.get_by_telegram_id = AsyncMock(return_value=MagicMock(id=1))
        await raw_admin_command(
            update, _make_context(["grant", "12345", "yearly"]), session, MagicMock()
        )

    assert "Unknown plan" in update.message.reply_text.call_args.args[0]


async def test_grant_rejects_unknown_user():
    update = make_update()
    session = MagicMock()

    with patch("app.bot.handlers.admin.UserRepository") as MockUserRepo:
        MockUserRepo.return_value.get_by_telegram_id = AsyncMock(return_value=None)
        await raw_admin_command(
            update, _make_context(["grant", "999999", "weekly"]), session, MagicMock()
        )

    assert "No user" in update.message.reply_text.call_args.args[0]


async def test_grant_success_commits_and_replies():
    update = make_update()
    session = MagicMock()
    session.commit = AsyncMock()
    target = MagicMock(id=1)
    sub = MagicMock()
    sub.expires_at.strftime.return_value = "2026-01-01 00:00 UTC"

    with patch("app.bot.handlers.admin.UserRepository") as MockUserRepo, \
         patch("app.bot.handlers.admin.SubscriptionService") as MockSubSvc:
        MockUserRepo.return_value.get_by_telegram_id = AsyncMock(return_value=target)
        MockSubSvc.return_value.grant_subscription = AsyncMock(return_value=sub)

        await raw_admin_command(
            update, _make_context(["grant", "12345", "weekly"]), session, MagicMock()
        )

        MockSubSvc.return_value.grant_subscription.assert_awaited_once_with(
            1, SubscriptionPlan.WEEKLY
        )
    session.commit.assert_awaited_once()
    assert "Granted" in update.message.reply_text.call_args.args[0]


async def test_revoke_requires_one_argument():
    update = make_update()
    await raw_admin_command(update, _make_context(["revoke"]), MagicMock(), MagicMock())
    assert "Usage" in update.message.reply_text.call_args.args[0]


async def test_revoke_success():
    update = make_update()
    session = MagicMock()
    session.commit = AsyncMock()
    target = MagicMock(id=1)

    with patch("app.bot.handlers.admin.UserRepository") as MockUserRepo, \
         patch("app.bot.handlers.admin.SubscriptionService") as MockSubSvc:
        MockUserRepo.return_value.get_by_telegram_id = AsyncMock(return_value=target)
        MockSubSvc.return_value.revoke_subscription = AsyncMock()

        await raw_admin_command(update, _make_context(["revoke", "12345"]), session, MagicMock())

    session.commit.assert_awaited_once()
    assert "Revoked" in update.message.reply_text.call_args.args[0]


async def test_revoke_no_subscription_replies_error_without_commit():
    update = make_update()
    session = MagicMock()
    session.commit = AsyncMock()
    target = MagicMock(id=1)

    with patch("app.bot.handlers.admin.UserRepository") as MockUserRepo, \
         patch("app.bot.handlers.admin.SubscriptionService") as MockSubSvc:
        MockUserRepo.return_value.get_by_telegram_id = AsyncMock(return_value=target)
        MockSubSvc.return_value.revoke_subscription = AsyncMock(
            side_effect=SubscriptionError("User has no subscription to revoke.")
        )

        await raw_admin_command(update, _make_context(["revoke", "12345"]), session, MagicMock())

    session.commit.assert_not_awaited()
    assert "❌" in update.message.reply_text.call_args.args[0]
