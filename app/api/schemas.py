"""
app.api.schemas
~~~~~~~~~~~~~~~
Pydantic response schemas for the REST API.

All schemas use `from_attributes=True` (SQLAlchemy ORM mode).
Sensitive fields (session strings, raw API keys, password hashes) are
NEVER included in any schema.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class _Base(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class UserSchema(_Base):
    id: int
    telegram_user_id: int
    username: str | None
    first_name: str | None
    last_name: str | None
    is_admin: bool
    created_at: datetime


class ApiKeySchema(_Base):
    """Returned once after key rotation. The raw key will never be visible again."""
    api_key: str = Field(..., description="Raw API key — store this securely. It will not be shown again.")


# ---------------------------------------------------------------------------
# TelegramAccount
# ---------------------------------------------------------------------------

class AccountSchema(_Base):
    id: int
    phone_masked: str
    status: str
    created_at: datetime


# ---------------------------------------------------------------------------
# Subscription
# ---------------------------------------------------------------------------

class SubscriptionSchema(_Base):
    id: int
    tier: str
    status: str
    expires_at: datetime
    created_at: datetime


# ---------------------------------------------------------------------------
# MediaRecord
# ---------------------------------------------------------------------------

class MediaRecordSchema(_Base):
    id: int
    telegram_account_id: int
    source_chat_id: int
    source_chat_title: str | None
    source_message_id: int
    sender_display_name: str | None
    sender_username: str | None
    media_type: str
    ttl_seconds: int | None
    status: str
    saved_message_id: int | None
    saved_at: datetime | None
    created_at: datetime


class PaginatedMediaSchema(_Base):
    items: list[MediaRecordSchema]
    total: int
    limit: int
    offset: int


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class AdminUserSchema(UserSchema):
    """Extended user schema for admin views — same fields for now."""
    pass
