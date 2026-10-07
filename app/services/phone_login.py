"""Secure, short-lived state for browser-based Telegram phone login.

This module only coordinates login state. It deliberately does NOT store a
Telethon client, OTP, 2FA password, API hash, phone number, or session.

Security properties:
- 256-bit+ random bearer token generated with secrets.
- Only a SHA-256 token hash is stored in Redis.
- Redis TTL provides automatic expiry (10 minutes by default).
- A token is single-use via Redis GETDEL.
- State is bound to the application user, Telegram user, and account row.
- Login attempts are explicitly staged so later endpoints can enforce the
  expected sequence (phone -> code -> optional 2FA -> complete).
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any

from app.core.redis import get_redis_client

PHONE_LOGIN_TTL_SECONDS = 10 * 60
PHONE_LOGIN_TOKEN_BYTES = 32
PHONE_LOGIN_KEY_PREFIX = "phone-login:"


class PhoneLoginError(Exception):
    """Base error for browser phone-login state handling."""


class PhoneLoginUnavailableError(PhoneLoginError):
    """Raised when Redis is unavailable for a phone-login attempt."""


class PhoneLoginNotFoundError(PhoneLoginError):
    """Raised when a login ticket is missing or expired."""


class PhoneLoginBindingError(PhoneLoginError):
    """Raised when a ticket is presented for the wrong application user."""


@dataclass(frozen=True)
class PhoneLoginState:
    """Serializable state for exactly one browser login attempt."""

    user_id: int
    telegram_user_id: int
    account_id: int | None
    stage: str = "phone"
    attempts: int = 0


class PhoneLoginCoordinator:
    """Issue, read, update, and consume short-lived phone-login tickets."""

    def __init__(self, *, ttl_seconds: int = PHONE_LOGIN_TTL_SECONDS) -> None:
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @classmethod
    def _redis_key(cls, token: str) -> str:
        return f"{PHONE_LOGIN_KEY_PREFIX}{cls._hash_token(token)}"

    @staticmethod
    def _serialize(state: PhoneLoginState) -> str:
        return json.dumps(
            {
                "user_id": state.user_id,
                "telegram_user_id": state.telegram_user_id,
                "account_id": state.account_id,
                "stage": state.stage,
                "attempts": state.attempts,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def _deserialize(raw: str) -> PhoneLoginState:
        try:
            data: dict[str, Any] = json.loads(raw)
            return PhoneLoginState(
                user_id=int(data["user_id"]),
                telegram_user_id=int(data["telegram_user_id"]),
                account_id=(None if data.get("account_id") is None else int(data["account_id"])),
                stage=str(data["stage"]),
                attempts=int(data["attempts"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise PhoneLoginError("Stored phone-login state is invalid.") from exc

    @staticmethod
    def _client():
        client = get_redis_client()
        if client is None:
            raise PhoneLoginUnavailableError(
                "Redis is required for secure phone login state."
            )
        return client

    async def issue(
        self,
        *,
        user_id: int,
        telegram_user_id: int,
        account_id: int,
    ) -> str:
        """Create a new one-time browser ticket and return its plaintext token.

        The plaintext token is returned only to the caller so it can be put
        into the HTTPS login URL. Redis stores only its hash.
        """
        if user_id <= 0 or telegram_user_id <= 0 or (account_id is not None and account_id <= 0):
            raise ValueError("user_id, telegram_user_id and account_id must be positive")

        token = secrets.token_urlsafe(PHONE_LOGIN_TOKEN_BYTES)
        state = PhoneLoginState(
            user_id=user_id,
            telegram_user_id=telegram_user_id,
            account_id=account_id,
        )
        client = self._client()
        await client.set(
            self._redis_key(token),
            self._serialize(state),
            ex=self._ttl_seconds,
            nx=True,
        )
        return token

    async def get(self, *, token: str) -> PhoneLoginState:
        """Read an unconsumed ticket without consuming it."""
        if not token:
            raise PhoneLoginNotFoundError("Login ticket is missing.")

        raw = await self._client().get(self._redis_key(token))
        if raw is None:
            raise PhoneLoginNotFoundError(
                "Login ticket is invalid, expired, or already used."
            )
        return self._deserialize(raw)

    async def require_user(
        self,
        *,
        token: str,
        user_id: int,
        telegram_user_id: int,
    ) -> PhoneLoginState:
        """Validate that the ticket belongs to the initiating user."""
        state = await self.get(token=token)
        if state.user_id != user_id or state.telegram_user_id != telegram_user_id:
            raise PhoneLoginBindingError("Login ticket is not bound to this user.")
        return state

    async def bind_account(
        self,
        *,
        token: str,
        user_id: int,
        telegram_user_id: int,
        account_id: int,
    ) -> PhoneLoginState:
        """Bind the ticket to the newly-created Telegram account row."""
        if account_id <= 0:
            raise ValueError("account_id must be positive")
        state = await self.require_user(
            token=token,
            user_id=user_id,
            telegram_user_id=telegram_user_id,
        )
        updated = PhoneLoginState(
            user_id=state.user_id,
            telegram_user_id=state.telegram_user_id,
            account_id=account_id,
            stage=state.stage,
            attempts=state.attempts,
        )
        await self._client().set(
            self._redis_key(token),
            self._serialize(updated),
            ex=self._ttl_seconds,
            xx=True,
        )
        return updated

    async def set_stage(
        self,
        *,
        token: str,
        user_id: int,
        telegram_user_id: int,
        stage: str,
        attempts: int | None = None,
    ) -> PhoneLoginState:
        """Advance the state while preserving the original user binding."""
        state = await self.require_user(
            token=token,
            user_id=user_id,
            telegram_user_id=telegram_user_id,
        )
        if not stage or len(stage) > 32:
            raise ValueError("stage must be 1-32 characters")

        updated = PhoneLoginState(
            user_id=state.user_id,
            telegram_user_id=state.telegram_user_id,
            account_id=state.account_id,
            stage=stage,
            attempts=state.attempts if attempts is None else attempts,
        )
        client = self._client()
        await client.set(
            self._redis_key(token),
            self._serialize(updated),
            ex=self._ttl_seconds,
            xx=True,
        )
        return updated

    async def consume(
        self,
        *,
        token: str,
        user_id: int,
        telegram_user_id: int,
    ) -> PhoneLoginState:
        """Consume a ticket exactly once after validating its user binding."""
        await self.require_user(
            token=token,
            user_id=user_id,
            telegram_user_id=telegram_user_id,
        )

        # GETDEL makes the final consume operation atomic: concurrent requests
        # cannot both receive the same state.
        raw = await self._client().getdel(self._redis_key(token))
        if raw is None:
            raise PhoneLoginNotFoundError(
                "Login ticket is invalid, expired, or already used."
            )

        state = self._deserialize(raw)
        if state.user_id != user_id or state.telegram_user_id != telegram_user_id:
            raise PhoneLoginBindingError("Login ticket is not bound to this user.")
        return state
