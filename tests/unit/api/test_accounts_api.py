"""
tests/unit/api/test_accounts_api.py
API tests for /api/v1/accounts
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_list_accounts(api_client, user_a, account_for_user_a):
    """User can list their connected accounts."""
    _, raw_key = user_a
    resp = await api_client.get("/api/v1/accounts", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["id"] == account_for_user_a.id
    assert data[0]["phone_masked"] == "+62****1234"


async def test_disconnect_account_success(api_client, user_a, account_for_user_a, db_session):
    """User can disconnect their own account."""
    _, raw_key = user_a
    resp = await api_client.delete(
        f"/api/v1/accounts/{account_for_user_a.id}",
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 204

    # Verify its status was updated in DB
    from app.db.repositories.telegram_account_repo import TelegramAccountRepository
    from app.db.models.telegram_account import TelegramAccountStatus
    repo = TelegramAccountRepository(db_session)
    account = await repo.get_by_id(account_for_user_a.id)
    assert account is not None
    assert account.status == TelegramAccountStatus.DISCONNECTED


async def test_disconnect_account_not_found(api_client, user_a):
    """Attempting to disconnect a non-existent account returns 404."""
    _, raw_key = user_a
    resp = await api_client.delete(
        "/api/v1/accounts/9999",
        headers={"X-API-Key": raw_key},
    )
    assert resp.status_code == 404
