"""
tests/unit/api/test_tenant_isolation.py
CRITICAL: Tenant isolation — User A must never access User B's data.
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_user_b_cannot_see_user_a_accounts(
    api_client, user_a, user_b, account_for_user_a
):
    """User B's account list must not include User A's accounts."""
    _, key_b = user_b
    resp = await api_client.get("/api/v1/accounts", headers={"X-API-Key": key_b})
    assert resp.status_code == 200
    accounts = resp.json()
    account_ids = [a["id"] for a in accounts]
    assert account_for_user_a.id not in account_ids


async def test_user_b_cannot_delete_user_a_account(
    api_client, user_a, user_b, account_for_user_a
):
    """User B cannot disconnect User A's account — must get 404."""
    _, key_b = user_b
    resp = await api_client.delete(
        f"/api/v1/accounts/{account_for_user_a.id}",
        headers={"X-API-Key": key_b},
    )
    assert resp.status_code == 404


async def test_user_b_cannot_see_user_a_media(
    api_client, user_a, user_b, media_for_user_a
):
    """User B cannot see User A's media record by ID — must get 404."""
    _, key_b = user_b
    resp = await api_client.get(
        f"/api/v1/media/{media_for_user_a.id}",
        headers={"X-API-Key": key_b},
    )
    assert resp.status_code == 404


async def test_user_b_media_list_is_empty(api_client, user_b, media_for_user_a):
    """User B's media list must be empty even though User A has records."""
    _, key_b = user_b
    resp = await api_client.get("/api/v1/media", headers={"X-API-Key": key_b})
    assert resp.status_code == 200
    assert resp.json()["items"] == []
