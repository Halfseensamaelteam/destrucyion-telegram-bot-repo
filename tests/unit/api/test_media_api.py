"""
tests/unit/api/test_media_api.py
API tests for /api/v1/media
"""

import pytest

pytestmark = pytest.mark.asyncio


async def test_list_media(api_client, user_a, media_for_user_a):
    """User can list their media records with pagination."""
    _, raw_key = user_a
    resp = await api_client.get("/api/v1/media", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["items"][0]["id"] == media_for_user_a.id
    assert data["items"][0]["source_chat_id"] == 100


async def test_list_media_pagination(api_client, user_a, db_session, account_for_user_a, media_for_user_a):
    """Verify pagination limit and offset work."""
    user, raw_key = user_a
    
    # Create a second record
    from app.db.models.media_record import MediaRecord, MediaType, MediaRecordStatus
    record2 = MediaRecord(
        user_id=user.id,
        telegram_account_id=account_for_user_a.id,
        source_chat_id=200,
        source_message_id=43,
        media_type=MediaType.VIDEO,
        status=MediaRecordStatus.SAVED,
    )
    db_session.add(record2)
    await db_session.commit()
    
    # Test limit=1, offset=0
    resp1 = await api_client.get("/api/v1/media?limit=1&offset=0", headers={"X-API-Key": raw_key})
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert len(data1["items"]) == 1
    
    # Test limit=1, offset=1
    resp2 = await api_client.get("/api/v1/media?limit=1&offset=1", headers={"X-API-Key": raw_key})
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert len(data2["items"]) == 1
    assert data1["items"][0]["id"] != data2["items"][0]["id"]


async def test_get_media_record_success(api_client, user_a, media_for_user_a):
    """User can fetch a single media record by ID."""
    _, raw_key = user_a
    resp = await api_client.get(f"/api/v1/media/{media_for_user_a.id}", headers={"X-API-Key": raw_key})
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == media_for_user_a.id


async def test_get_media_record_not_found(api_client, user_a):
    """Fetching a non-existent record returns 404."""
    _, raw_key = user_a
    resp = await api_client.get("/api/v1/media/9999", headers={"X-API-Key": raw_key})
    assert resp.status_code == 404
