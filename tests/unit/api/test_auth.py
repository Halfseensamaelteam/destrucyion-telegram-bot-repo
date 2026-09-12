"""
tests/unit/api/test_auth.py
Authentication tests: 401 / 200 behavior.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_health_is_public(api_client):
    """Health endpoint requires no auth."""
    resp = await api_client.get("/api/health")
    assert resp.status_code == 200


async def test_me_without_key_returns_401(api_client):
    resp = await api_client.get("/api/v1/me")
    assert resp.status_code == 401


async def test_me_with_wrong_key_returns_401(api_client):
    resp = await api_client.get("/api/v1/me", headers={"X-API-Key": "wrong_key"})
    assert resp.status_code == 401


async def test_me_with_valid_key_returns_200(api_client, user_a):
    user, raw_key = user_a
    resp = await api_client.get("/api/v1/me", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == user.id
    assert data["username"] == "user_a"
    assert "api_key_hash" not in data  # Never expose the hash


async def test_rotate_api_key_returns_new_key(api_client, user_a):
    _, raw_key = user_a
    resp = await api_client.post("/api/v1/me/apikey", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    data = resp.json()
    assert "api_key" in data
    assert len(data["api_key"]) == 64  # 32-byte hex


async def test_old_key_invalid_after_rotation(api_client, user_a):
    """After rotation the old key must no longer work."""
    _, old_key = user_a
    rotate_resp = await api_client.post("/api/v1/me/apikey", headers={"X-API-Key": old_key})
    assert rotate_resp.status_code == 200

    # Old key is now invalid
    resp = await api_client.get("/api/v1/me", headers={"X-API-Key": old_key})
    assert resp.status_code == 401
