"""
tests/unit/bot/test_subscription.py
Tests for /subscription — previously had NO coverage, which is how the
`expires_at.strftime()` crash on lifetime plans (expires_at=None) shipped.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.unit.bot.test_dependencies import make_update
from app.bot.handlers.subscription import subscription_command
from app.db.models.subscription import SubscriptionPlan, SubscriptionStatus

pytestmark = pytest.mark.asyncio

raw_subscription_command = subscription_command.__wrapped__


def make_subscription(plan=SubscriptionPlan.WEEKLY, status=SubscriptionStatus.ACTIVE, expires_at=None):
    sub = MagicMock()
    sub.plan = plan
    sub.status = status
    sub.expires_at = expires_at
    return sub


async def test_no_subscription_shows_free_tier_message():
    update = make_update()
    session = MagicMock()

    with patch("app.bot.handlers.subscription.SubscriptionRepository") as MockRepo:
        MockRepo.return_value.get_by_user_id = AsyncMock(return_value=None)
        await raw_subscription_command(update, MagicMock(), session, MagicMock(id=1))

    text = update.message.reply_text.call_args.args[0]
    assert "do not have an active subscription" in text


async def test_lifetime_subscription_does_not_crash_on_none_expiry():
    """Regression test: lifetime plans have expires_at=None by design —
    calling .strftime() on None used to raise AttributeError and silently
    kill the command with no reply to the user."""
    update = make_update()
    session = MagicMock()
    sub = make_subscription(plan=SubscriptionPlan.LIFETIME, expires_at=None)

    with patch("app.bot.handlers.subscription.SubscriptionRepository") as MockRepo:
        MockRepo.return_value.get_by_user_id = AsyncMock(return_value=sub)
        await raw_subscription_command(update, MagicMock(), session, MagicMock(id=1))

    text = update.message.reply_text.call_args.args[0]
    assert "Never" in text
    assert "Lifetime" in text.title() or "lifetime" in text.lower()


async def test_weekly_subscription_shows_expiry_date():
    import datetime

    update = make_update()
    session = MagicMock()
    sub = make_subscription(
        plan=SubscriptionPlan.WEEKLY,
        expires_at=datetime.datetime(2026, 12, 25, 10, 0, tzinfo=datetime.timezone.utc),
    )

    with patch("app.bot.handlers.subscription.SubscriptionRepository") as MockRepo:
        MockRepo.return_value.get_by_user_id = AsyncMock(return_value=sub)
        await raw_subscription_command(update, MagicMock(), session, MagicMock(id=1))

    text = update.message.reply_text.call_args.args[0]
    assert "2026-12-25" in text
