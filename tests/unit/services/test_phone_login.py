"""Tests for secure browser phone-login state."""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from app.core.redis import override_redis_client, reset_redis_client
from app.services.phone_login import (
    PhoneLoginBindingError,
    PhoneLoginCoordinator,
    PhoneLoginNotFoundError,
)


@pytest.fixture
def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    override_redis_client(client)
    yield client
    reset_redis_client()


@pytest.mark.asyncio
async def test_issue_stores_hashed_token_only(redis_client):
    coordinator = PhoneLoginCoordinator(ttl_seconds=600)
    token = await coordinator.issue(user_id=7, telegram_user_id=700, account_id=70)

    assert len(token) >= 40
    state = await coordinator.get(token=token)
    assert state.user_id == 7
    assert state.telegram_user_id == 700
    assert state.account_id == 70
    assert state.stage == "phone"

    keys = await redis_client.keys("phone-login:*")
    assert keys
    assert token not in keys[0]


@pytest.mark.asyncio
async def test_ticket_is_bound_to_user(redis_client):
    coordinator = PhoneLoginCoordinator(ttl_seconds=600)
    token = await coordinator.issue(user_id=7, telegram_user_id=700, account_id=70)

    with pytest.raises(PhoneLoginBindingError):
        await coordinator.require_user(token=token, user_id=8, telegram_user_id=800)


@pytest.mark.asyncio
async def test_consume_is_single_use(redis_client):
    coordinator = PhoneLoginCoordinator(ttl_seconds=600)
    token = await coordinator.issue(user_id=7, telegram_user_id=700, account_id=70)

    state = await coordinator.consume(token=token, user_id=7, telegram_user_id=700)
    assert state.account_id == 70

    with pytest.raises(PhoneLoginNotFoundError):
        await coordinator.get(token=token)


@pytest.mark.asyncio
async def test_stage_update_preserves_binding(redis_client):
    coordinator = PhoneLoginCoordinator(ttl_seconds=600)
    token = await coordinator.issue(user_id=7, telegram_user_id=700, account_id=70)

    state = await coordinator.set_stage(
        token=token,
        user_id=7,
        telegram_user_id=700,
        stage="code",
        attempts=1,
    )

    assert state.stage == "code"
    assert state.attempts == 1
    assert state.user_id == 7
