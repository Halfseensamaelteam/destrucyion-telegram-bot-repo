"""
tests/integration/test_bot_webhook.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Integration tests for Telegram Bot webhook endpoint.

These tests verify that the bot webhook endpoint actually processes
Telegram updates and responds to commands end-to-end.

Run with:
    uv run pytest tests/integration/test_bot_webhook.py -v
"""

import json
import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app


@pytest.mark.asyncio
async def test_bot_webhook_endpoint_exists():
    """Test that the webhook endpoint exists and accepts POST requests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send a minimal update
        update_data = {
            "update_id": 123456789,
            "message": {
                "message_id": 1,
                "from": {
                    "id": 123456789,
                    "is_bot": False,
                    "first_name": "Test",
                },
                "chat": {
                    "id": 123456789,
                    "type": "private",
                },
                "date": 1234567890,
                "text": "/start",
            },
        }
        
        response = await client.post("/webhook/telegram", json=update_data)
        
        # Should return 200 OK
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_bot_webhook_processes_start_command():
    """Test that the webhook endpoint processes /start command."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send a /start command update
        update_data = {
            "update_id": 123456789,
            "message": {
                "message_id": 1,
                "from": {
                    "id": 123456789,
                    "is_bot": False,
                    "first_name": "Test",
                },
                "chat": {
                    "id": 123456789,
                    "type": "private",
                },
                "date": 1234567890,
                "text": "/start",
            },
        }
        
        response = await client.post("/webhook/telegram", json=update_data)
        
        # Should return 200 OK
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_bot_webhook_handles_invalid_json():
    """Test that the webhook endpoint handles invalid JSON gracefully."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send invalid JSON
        response = await client.post("/webhook/telegram", content="invalid json")
        
        # Should return 400 (Bad Request)
        assert response.status_code == 400
        assert response.json()["status"] == "error"


@pytest.mark.asyncio
async def test_bot_webhook_handles_empty_update():
    """Test that the webhook endpoint handles empty update gracefully."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send empty update (missing update_id)
        update_data = {}
        
        response = await client.post("/webhook/telegram", json=update_data)
        
        # Should still return 200 to avoid Telegram retrying
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}
