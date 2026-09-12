"""
tests/unit/api/test_admin_api.py
API tests for /api/v1/admin
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_admin_list_users_success(api_client, admin_user, user_a):
    """Admin can list all users."""
    _, admin_key = admin_user
    resp = await api_client.get("/api/v1/admin/users", headers={"X-API-Key": admin_key})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) >= 2  # Admin + User A
    
    usernames = [u["username"] for u in data]
    assert "admin" in usernames
    assert "user_a" in usernames


async def test_non_admin_cannot_list_users(api_client, user_a):
    """Non-admin gets 403 Forbidden on admin endpoints."""
    _, raw_key = user_a
    resp = await api_client.get("/api/v1/admin/users", headers={"X-API-Key": raw_key})
    assert resp.status_code == 403


async def test_admin_grant_subscription(api_client, admin_user, user_a, db_session):
    """Admin can manually grant a subscription to a user."""
    _, admin_key = admin_user
    user, _ = user_a
    
    payload = {
        "plan": "lifetime",
        "days": 365
    }
    resp = await api_client.post(
        f"/api/v1/admin/users/{user.id}/grant-sub",
        headers={"X-API-Key": admin_key},
        json=payload
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["tier"] == "lifetime"
    assert data["status"] == "active"
    
    # Verify in DB
    from app.db.repositories.subscription_repo import SubscriptionRepository
    repo = SubscriptionRepository(db_session)
    sub = await repo.get_by_user_id(user.id)
    assert sub is not None
    assert sub.plan.value == "lifetime"


async def test_admin_grant_subscription_user_not_found(api_client, admin_user):
    """Granting subscription to non-existent user returns 404."""
    _, admin_key = admin_user
    resp = await api_client.post(
        "/api/v1/admin/users/9999/grant-sub",
        headers={"X-API-Key": admin_key},
        json={"plan": "monthly", "days": 30}
    )
    assert resp.status_code == 404
