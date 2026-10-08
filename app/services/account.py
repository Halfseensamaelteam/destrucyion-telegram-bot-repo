"""
app.services.account
~~~~~~~~~~~~~~~~~~~~~
Business logic for Telegram account authentication and session management.

Authentication flow (CLAUDE.md §12 — QR login ONLY, no phone+code):
    1. create_or_reset_account — get-or-create this user's single account
                                  row (DB has a UNIQUE constraint on
                                  user_id, CLAUDE.md §12.3), store THIS
                                  account's own api_id/api_hash (§12.2).
    2. perform_qr_login         — long-running: generate QR, wait for scan,
                                  auto-refresh expired tokens, handle
                                  optional 2FA, encrypt+persist session.
    3. load_session              — decrypt ciphertext → StringSession
    4. disconnect_account        — mark disconnected in DB

Security rules (CLAUDE.md §13):
    - Session strings and api_hash are NEVER logged.
    - Session strings and api_hash are NEVER returned from API endpoints.
    - Session strings and api_hash are NEVER stored unencrypted.
    - Only ciphertext touches the database.
    - Tenant isolation: every method validates user_id ownership.

Why QR login and not phone+code (CLAUDE.md §12.1):
    Telegram's abuse detection blocks "this code was previously shared"
    whenever a login code is typed into any chat (including a bot's chat)
    before being used to sign in. QR login has no equivalent problem
    because no code is ever typed anywhere — the user scans the QR with
    their own already-logged-in Telegram client.

CRITICAL implementation constraint — single continuous connection:
    The QR token, and the login it represents, is tied to the specific
    connection (auth key) that created it via qr_login(). This method
    holds ONE connected TelegramClient for the ENTIRE attempt — across QR
    token refreshes and the optional 2FA step. Disconnecting and creating
    a fresh client partway through (e.g. between "generate QR" and "wait
    for scan") reliably breaks the login, the exact same class of bug as
    the old phone+code flow's PhoneCodeExpiredError.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession

from app.core.crypto import SessionCipher
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.repositories.telegram_account_repo import TelegramAccountRepository

# How long, in total, we're willing to keep showing/refreshing a QR code
# before giving up on a single /connect attempt.
QR_LOGIN_TOTAL_TIMEOUT_SECONDS = 10 * 60
# Telethon's QR token itself expires after roughly this long; when wait()
# times out we recreate the token and show a fresh QR rather than failing.
QR_LOGIN_TOKEN_WAIT_SECONDS = 30
# How long to wait for the user to reply with their 2FA password before
# giving up on this attempt.
QR_LOGIN_PASSWORD_TIMEOUT_SECONDS = 2 * 60


class AccountNotFoundError(Exception):
    """Raised when an account does not exist or does not belong to the user."""


class AccountAlreadyExistsError(Exception):
    """Raised when a user already has a non-disconnected Telegram account.

    Enforces CLAUDE.md §12.3 — one Telegram account per application user.
    """


class AuthError(Exception):
    """Raised when Telegram authentication fails."""


class AuthTimeoutError(AuthError):
    """Raised when the QR login was not completed within the time budget."""


class SessionError(Exception):
    """Raised when a session cannot be loaded or decrypted."""


def _mask_phone(phone: str) -> str:
    """+6281234567890 -> +62********890 (keep country-code-ish prefix + last 3)."""
    if len(phone) <= 6:
        return "*" * len(phone)
    return phone[:3] + "*" * (len(phone) - 6) + phone[-3:]


@dataclass
class QrLoginCallbacks:
    """Hooks the caller (bot handler) provides to drive the QR login UI.

    All callbacks are awaited and may be called multiple times (e.g.
    on_qr_ready fires again each time the QR token is refreshed).
    """

    on_qr_ready: Callable[[str], Awaitable[None]]
    get_2fa_password: Callable[[], Awaitable[str]]


class AccountService:
    """Manages Telegram account creation, authentication, and session lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = TelegramAccountRepository(session)

    def get_full_phone(self, account: TelegramAccount) -> str | None:
        """Decrypt and return the FULL phone number for this account.

        Sensitive — only ever call this to build the operator's own admin
        notification (see app/telegram/admin_notify.py). Never log the
        result, never surface it in any customer-facing reply.
        """
        if not account.phone_ciphertext:
            return None
        cipher = SessionCipher.from_settings()
        return cipher.decrypt(account.phone_ciphertext)


    def _get_client(self, api_id: int, api_hash: str) -> TelegramClient:
        """Create a Telethon client using THIS ACCOUNT's own api_id/api_hash.

        CLAUDE.md §12.2 — there is no fallback to a global/operator api_id.
        Uses a fresh in-memory StringSession — never a file session. The
        caller owns connecting and disconnecting this client.
        """
        return TelegramClient(StringSession(), api_id, api_hash)

    async def _get_account_for_user(
        self, account_id: int, user_id: int
    ) -> TelegramAccount:
        account = await self._repo.get_by_id_and_user(account_id, user_id)
        if account is None:
            raise AccountNotFoundError(
                f"Account {account_id} not found for user {user_id}."
            )
        return account

    async def create_or_reset_account(
        self, *, user_id: int, api_id: int, api_hash: str
    ) -> TelegramAccount:
        """Get-or-create this user's single Telegram account row.

        The database enforces a UNIQUE constraint on user_id (CLAUDE.md
        §12.3) — there is at most one row per user, ever. If the user has
        no row yet, one is created. If they have a row and it is
        DISCONNECTED, it is reset in place with the new api_id/api_hash
        (any previous session is cleared). If they have a row that is NOT
        disconnected, this raises AccountAlreadyExistsError.

        Args:
            user_id: The application user creating/resetting the account.
            api_id: This account's OWN Telegram api_id (from my.telegram.org).
            api_hash: This account's OWN Telegram api_hash. Encrypted at rest.

        Raises:
            AccountAlreadyExistsError: User must /disconnect first.
        """
        cipher = SessionCipher.from_settings()
        existing = await self._repo.get_by_user_id(user_id)

        if existing is not None:
            if existing.status != TelegramAccountStatus.DISCONNECTED:
                raise AccountAlreadyExistsError(
                    "You already have a Telegram account connected (or a "
                    "connection in progress). Disconnect it first with "
                    "/disconnect before connecting a different one."
                )
            # Reset the existing row for reuse — clear any stale session
            # from the previous connection.
            return await self._repo.update(
                existing,
                api_id=api_id,
                api_hash_ciphertext=cipher.encrypt(api_hash),
                session_ciphertext=None,
                telegram_user_id=None,
                username=None,
                last_error=None,
            )

        return await self._repo.create(
            user_id=user_id,
            status=TelegramAccountStatus.DISCONNECTED,
            api_id=api_id,
            api_hash_ciphertext=cipher.encrypt(api_hash),
        )

    async def perform_qr_login(
        self,
        account_id: int,
        user_id: int,
        callbacks: QrLoginCallbacks,
    ) -> TelegramAccount:
        """Run the full QR login flow for account_id, start to finish.

        Holds ONE connected TelegramClient for the entire attempt — across
        QR token refreshes and the optional 2FA step. Intended to run as a
        background task (e.g. asyncio.create_task) from the /connect
        handler, since it can take up to QR_LOGIN_TOTAL_TIMEOUT_SECONDS.

        Raises:
            AccountNotFoundError: Tenant violation.
            AuthTimeoutError: QR was never scanned within the time budget,
                or the 2FA password was never supplied in time.
            AuthError: Any other Telegram authentication failure.
        """
        account = await self._get_account_for_user(account_id, user_id)
        if account.api_id is None or account.api_hash_ciphertext is None:
            raise AuthError(
                "This account has no api_id/api_hash on file. Use /connect "
                "again and supply your own credentials first."
            )

        cipher = SessionCipher.from_settings()
        api_hash = cipher.decrypt(account.api_hash_ciphertext)

        client = self._get_client(api_id=account.api_id, api_hash=api_hash)
        try:
            await client.connect()

            qr_login = await client.qr_login()
            await callbacks.on_qr_ready(qr_login.url)

            elapsed = 0.0
            me = None
            while elapsed < QR_LOGIN_TOTAL_TIMEOUT_SECONDS:
                try:
                    me = await qr_login.wait(timeout=QR_LOGIN_TOKEN_WAIT_SECONDS)
                    break
                except asyncio.TimeoutError:
                    elapsed += QR_LOGIN_TOKEN_WAIT_SECONDS
                    if elapsed >= QR_LOGIN_TOTAL_TIMEOUT_SECONDS:
                        raise AuthTimeoutError(
                            "QR code was not scanned in time. Please /connect again."
                        )
                    # Token expired without being scanned — refresh and show
                    # an updated QR on the SAME still-connected client.
                    await qr_login.recreate()
                    await callbacks.on_qr_ready(qr_login.url)
                except SessionPasswordNeededError:
                    try:
                        password = await asyncio.wait_for(
                            callbacks.get_2fa_password(),
                            timeout=QR_LOGIN_PASSWORD_TIMEOUT_SECONDS,
                        )
                    except asyncio.TimeoutError:
                        raise AuthTimeoutError(
                            "2FA password was not provided in time. "
                            "Please /connect again."
                        )
                    # Same still-connected client — required, or Telegram
                    # will not accept the sign-in (same reasoning as the
                    # original phone+code session-continuity bug).
                    await client.sign_in(password=password)
                    me = await client.get_me()
                    break

            if me is None:
                raise AuthError("QR login did not complete for an unknown reason.")

            # SECURITY: encrypt immediately, never let the raw session
            # string escape this scope.
            session_ciphertext = cipher.encrypt(client.session.save())

        except (AccountNotFoundError, AuthTimeoutError, AuthError):
            raise
        except RPCError as exc:
            raise AuthError(f"Telegram authentication error: {exc}") from exc
        except Exception as exc:
            raise AuthError(f"Authentication failed unexpectedly: {exc}") from exc
        finally:
            await client.disconnect()

        phone_masked = _mask_phone(me.phone) if me and me.phone else None
        phone_ciphertext = cipher.encrypt(me.phone) if me and me.phone else None

        return await self._repo.update_session(
            account,
            session_ciphertext=session_ciphertext,
            telegram_user_id=me.id if me else None,
            username=me.username if me else None,
            phone_masked=phone_masked,
            phone_ciphertext=phone_ciphertext,
        )

    async def try_resume_session(self, account_id: int, user_id: int) -> bool:
        """Try to resume this account's PREVIOUSLY stored session, without a
        full QR login. Used on /connect when a DISCONNECTED account already
        has a session_ciphertext on file (e.g. the user only ran
        /disconnect locally and never actually unlinked the device in
        Telegram's own Settings > Devices).

        Connects using the stored session + this account's own api_id/hash,
        checks client.is_user_authorized(). If still valid, marks the
        account ACTIVE again immediately and returns True — no QR needed.
        If the session was revoked (unlinked from another device, password
        changed, etc.), returns False so the caller can fall back to a
        fresh QR login; this is normal and expected, not an error.

        Raises:
            AccountNotFoundError: Tenant violation.
        """
        account = await self._get_account_for_user(account_id, user_id)
        if not account.session_ciphertext or not account.api_id or not account.api_hash_ciphertext:
            return False  # nothing to resume

        cipher = SessionCipher.from_settings()
        session_string = cipher.decrypt(account.session_ciphertext)
        api_hash = cipher.decrypt(account.api_hash_ciphertext)

        client = TelegramClient(StringSession(session_string), account.api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return False

            me = await client.get_me()
        except Exception:
            # Any failure here (network, revoked auth key, etc.) just means
            # "can't resume" — fall back to QR login, don't raise.
            return False
        finally:
            await client.disconnect()

        await self._repo.update_status(account, TelegramAccountStatus.ACTIVE, last_error=None)
        await self._repo.update(
            account, telegram_user_id=me.id if me else None, username=me.username if me else None
        )
        return True

    async def load_session(self, account_id: int, user_id: int) -> StringSession:
        """Decrypt and return the StringSession for a connected account."""
        account = await self._get_account_for_user(account_id, user_id)
        if not account.session_ciphertext:
            raise SessionError(
                f"Account {account_id} has no stored session. Please authenticate first."
            )
        cipher = SessionCipher.from_settings()
        return StringSession(cipher.decrypt(account.session_ciphertext))

    def get_account_api_credentials(self, account: TelegramAccount) -> tuple[int, str]:
        """Decrypt and return (api_id, api_hash) for THIS account.

        Used by the worker (CLAUDE.md §12.2) to build the per-account
        Telethon client instead of any global/operator credentials.
        """
        if account.api_id is None or account.api_hash_ciphertext is None:
            raise SessionError(f"Account {account.id} has no api_id/api_hash on file.")
        cipher = SessionCipher.from_settings()
        return account.api_id, cipher.decrypt(account.api_hash_ciphertext)

    async def delete_account(self, account_id: int, user_id: int) -> None:
        """Permanently remove this user-owned Telegram account row."""
        account = await self._get_account_for_user(account_id, user_id)
        await self._repo.delete(account)

    async def disconnect_account(self, account_id: int, user_id: int) -> TelegramAccount:
        """Mark an account as disconnected. Session ciphertext is preserved
        (in case of accidental disconnect) but will be cleared on the next
        /connect via create_or_reset_account."""
        account = await self._get_account_for_user(account_id, user_id)
        return await self._repo.update_status(
            account, TelegramAccountStatus.DISCONNECTED, last_error=None
        )