"""
app.services.api_key
~~~~~~~~~~~~~~~~~~~~
API Key generation, hashing, and rotation.

Security rules:
- The raw API key is NEVER stored anywhere — only its SHA-256 hash.
- The raw key is only returned once: at generation/rotation time.
- The hash is stored in users.api_key_hash (String(64), unique).
- Lookup is always by hash: SELECT WHERE api_key_hash = hash(raw_key).
"""

from __future__ import annotations

import hashlib
import secrets

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.user_repo import UserRepository

log = get_logger(__name__)

# Length of the raw API key in bytes → 32 bytes = 64 hex chars
_KEY_BYTES = 32


def generate_api_key() -> str:
    """Generate a cryptographically secure random API key (hex string, 64 chars)."""
    return secrets.token_hex(_KEY_BYTES)


def hash_api_key(raw_key: str) -> str:
    """Return the SHA-256 hex digest of the raw key.

    This is what gets stored in the database.
    The raw key must NEVER be stored — only the hash.
    """
    return hashlib.sha256(raw_key.encode()).hexdigest()


class ApiKeyService:
    """Manages API key rotation for application users."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = UserRepository(session)

    async def rotate(self, user: User) -> str:
        """Generate a new API key for the user, store the hash, and return the raw key.

        The returned raw key is the ONLY opportunity the caller has to see it.
        After this call, only the hash is stored.

        Returns:
            The new raw API key (64 hex chars).
        """
        raw_key = generate_api_key()
        key_hash = hash_api_key(raw_key)

        await self._repo.update(user, api_key_hash=key_hash)
        log.info("api_key_rotated", user_id=user.id)

        return raw_key

    async def get_user_by_key(self, raw_key: str) -> User | None:
        """Look up a user by their raw API key.

        Hashes the key internally, never exposes the hash to the caller.

        Returns:
            The User if found, None otherwise.
        """
        key_hash = hash_api_key(raw_key)
        return await self._repo.get_by_api_key_hash(key_hash)
