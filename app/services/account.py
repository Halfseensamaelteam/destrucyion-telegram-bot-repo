"""
app.services.account
~~~~~~~~~~~~~~~~~~~~~
Business logic for Telegram account authentication and session management.

Authentication flow:
    1. create_account  — create DB row, mask phone number
    2. start_auth      — send_code_request via Telethon, return phone_code_hash
    3. verify_code     — sign_in, encrypt session, persist ciphertext
    4. load_session    — decrypt ciphertext → StringSession (do NOT log)
    5. disconnect_account — mark disconnected in DB

Security rules (CLAUDE.md §13):
    - Session strings are NEVER logged.
    - Session strings are NEVER returned from API endpoints.
    - Session strings are NEVER stored unencrypted.
    - Only ciphertext touches the database.
    - Tenant isolation: every method validates user_id ownership.
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
    RPCError,
)
from telethon.sessions import StringSession
import qrcode

from app.core.config import get_settings
from app.core.crypto import SessionCipher
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.repositories.telegram_account_repo import TelegramAccountRepository


class AccountNotFoundError(Exception):
    """Raised when an account does not exist or does not belong to the user."""


class AuthError(Exception):
    """Raised when Telegram authentication fails."""


class SessionError(Exception):
    """Raised when a session cannot be loaded or decrypted."""


def _mask_phone(phone: str) -> str:
    """Mask a phone number, keeping only the last 4 digits.

    Example: +628123456789 → +62*******6789
    """
    digits = re.sub(r"[^\d+]", "", phone)
    if len(digits) <= 4:
        return "*" * len(digits)
    return digits[: len(digits) - 8] + "****" + digits[-4:]


@dataclass
class AuthStartResult:
    """Result of start_auth — only contains data safe to pass around."""

    account_id: int
    phone_code_hash: str  # Required by Telegram to verify the code
    temp_session: str
    # The exact StringSession used for send_code_request(). Telegram ties the
    # login code to the specific auth key/connection that requested it — a
    # fresh client+session for sign_in() will fail with PhoneCodeExpiredError
    # even with a valid phone_code_hash. The caller MUST pass this same
    # string back into verify_code(). It is pre-authentication (no user is
    # logged in yet) but should still never be logged or persisted long-term.


@dataclass
class QRAuthResult:
    """Result of QR authentication start."""

    account_id: int
    qr_code_bytes: bytes  # PNG image bytes
    login_obj: object  # Telethon QRLogin object for monitoring completion


class AccountService:
    """Manages Telegram account creation, authentication, and session lifecycle."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._repo = TelegramAccountRepository(session)

    def _get_client(
        self,
        string_session: str | None = None,
        api_id: int | None = None,
        api_hash: str | None = None,
    ) -> TelegramClient:
        """Create a Telethon client.

        Uses MemorySession (or StringSession if provided) — never a file session.
        The caller is responsible for connecting and disconnecting.

        Args:
            string_session: Optional StringSession string for reusing sessions.
            api_id: Optional Telegram API ID (falls back to settings if None).
            api_hash: Optional Telegram API hash (falls back to settings if None).
        """
        settings = get_settings()
        tg_session = StringSession(string_session) if string_session else StringSession()
        return TelegramClient(
            tg_session,
            api_id if api_id is not None else settings.telegram_api_id,
            api_hash if api_hash is not None else settings.telegram_api_hash,
        )

    async def _get_account_for_user(
        self, account_id: int, user_id: int
    ) -> TelegramAccount:
        """Tenant-safe account lookup. Raises AccountNotFoundError if not found."""
        account = await self._repo.get_by_id_and_user(account_id, user_id)
        if account is None:
            raise AccountNotFoundError(
                f"Account {account_id} not found for user {user_id}."
            )
        return account

    async def create_account(
        self, *, user_id: int, phone_number: str
    ) -> TelegramAccount:
        """Create a new TelegramAccount DB row in DISCONNECTED state.

        The phone number is masked before storing.
        """
        masked = _mask_phone(phone_number)
        account = await self._repo.create(
            user_id=user_id,
            phone_masked=masked,
            status=TelegramAccountStatus.DISCONNECTED,
        )
        return account

    async def start_auth(
        self,
        account_id: int,
        user_id: int,
        phone_number: str,
    ) -> AuthStartResult:
        """Send a Telegram login code to the given phone number.

        The phone_code_hash MUST be stored temporarily by the caller (e.g. in
        Redis or bot conversation state) and passed to verify_code. It is NOT
        persisted in the database.

        Args:
            account_id: The TelegramAccount row to authenticate.
            user_id: Must own account_id (tenant isolation).
            phone_number: The full international phone number.

        Returns:
            AuthStartResult with account_id and phone_code_hash.

        Raises:
            AccountNotFoundError: If account does not belong to user_id.
            AuthError: If Telegram rejects the code request.
        """
        account = await self._get_account_for_user(account_id, user_id)

        client = self._get_client()
        try:
            await client.connect()
            result = await client.send_code_request(phone_number)
            # CRITICAL: capture the session BEFORE disconnecting. This is the
            # exact auth key/connection the code was issued against — reusing
            # it in verify_code() is required or Telegram will reject the
            # code as expired regardless of correctness. See docs/ROADMAP.md
            # Phase 5 for the full explanation.
            temp_session = client.session.save()
        except Exception as exc:
            raise AuthError(f"Failed to send code: {exc}") from exc
        finally:
            await client.disconnect()

        # Update status to show auth is in progress
        await self._repo.update_status(account, TelegramAccountStatus.DISCONNECTED)

        return AuthStartResult(
            account_id=account_id,
            phone_code_hash=result.phone_code_hash,
            temp_session=temp_session,
        )

    async def verify_code(
        self,
        account_id: int,
        user_id: int,
        phone_number: str,
        code: str,
        phone_code_hash: str,
        temp_session: str,
        password: str | None = None,
    ) -> TelegramAccount:
        """Verify the login code (and optional 2FA password).

        On success:
            1. Serializes the session via StringSession.
            2. Encrypts it with SessionCipher.
            3. Stores the ciphertext in the database.
            4. Sets account status to ACTIVE.

        The raw session string is NEVER returned to callers,
        NEVER stored anywhere except as ciphertext, and
        NEVER logged.

        Args:
            account_id: The account being authenticated.
            user_id: Must own account_id.
            phone_number: Must match what was used in start_auth.
            code: The 5-6 digit code received by the user.
            phone_code_hash: The hash returned by start_auth.
            temp_session: The EXACT StringSession string returned by
                start_auth's AuthStartResult.temp_session. Reusing this same
                session (same auth key) is required — a fresh empty session
                here will make Telegram reject the code as expired even if
                it's correct. See docs/ROADMAP.md Phase 5.
            password: 2FA cloud password, if applicable.

        Returns:
            The updated TelegramAccount (status=ACTIVE, session stored).

        Raises:
            AccountNotFoundError: Tenant violation.
            AuthError: Wrong code, expired code, wrong password, etc.
        """
        account = await self._get_account_for_user(account_id, user_id)

        # CRITICAL: must reuse the exact session from start_auth — a fresh
        # empty session here will always fail with PhoneCodeExpiredError,
        # even for a correct/unexpired code. See docs/ROADMAP.md Phase 5.
        client = self._get_client(string_session=temp_session)
        try:
            await client.connect()

            # If password is provided, skip code sign-in and go directly to password sign-in
            # This avoids "code previously shared" error when retrying with 2FA
            if password:
                await client.sign_in(password=password)
            else:
                try:
                    await client.sign_in(
                        phone=phone_number,
                        code=code,
                        phone_code_hash=phone_code_hash,
                    )
                except SessionPasswordNeededError:
                    raise AuthError(
                        "2FA is enabled on this account. A cloud password is required."
                    )
                except PhoneCodeInvalidError:
                    raise AuthError("The login code is incorrect.")
                except PhoneCodeExpiredError:
                    raise AuthError("The login code has expired. Please request a new one.")
                except RPCError as e:
                    # Catch all other Telegram RPC errors and surface them
                    raise AuthError(f"Telegram authentication error: {e}") from e

            # --- Serialize session ---
            # SECURITY: This is the only place the raw session string is used.
            # It must be encrypted immediately and never assigned to a variable
            # that could be referenced beyond this scope.
            me = await client.get_me()
            cipher = SessionCipher.from_settings()
            ciphertext = cipher.encrypt(client.session.save())

        except (AccountNotFoundError, AuthError):
            raise  # re-raise without wrapping
        except Exception as exc:
            raise AuthError(f"Authentication failed unexpectedly: {exc}") from exc
        finally:
            await client.disconnect()

        # Persist encrypted session — raw session is out of scope here
        account = await self._repo.update_session(
            account,
            session_ciphertext=ciphertext,
            telegram_user_id=me.id if me else None,
            username=me.username if me else None,
        )
        return account

    async def load_session(self, account_id: int, user_id: int) -> StringSession:
        """Decrypt and return the StringSession for a connected account.

        The returned StringSession is ready to pass directly to TelegramClient.
        The caller MUST NOT log its value.

        Raises:
            AccountNotFoundError: Tenant violation.
            SessionError: No session stored or decryption failed.
        """
        account = await self._get_account_for_user(account_id, user_id)

        if not account.session_ciphertext:
            raise SessionError(
                f"Account {account_id} has no stored session. Please authenticate first."
            )

        cipher = SessionCipher.from_settings()
        # Decrypt — do NOT log the result
        session_string = cipher.decrypt(account.session_ciphertext)
        return StringSession(session_string)

    async def disconnect_account(
        self, account_id: int, user_id: int
    ) -> TelegramAccount:
        """Mark an account as disconnected in the database.

        Does NOT revoke the Telegram session on the server side.
        The session ciphertext is preserved so reconnection is possible.
        """
        account = await self._get_account_for_user(account_id, user_id)
        return await self._repo.update_status(
            account,
            TelegramAccountStatus.DISCONNECTED,
            last_error=None,
        )

    async def start_qr_auth(
        self,
        account_id: int,
        user_id: int,
        api_id: int,
        api_hash: str,
    ) -> QRAuthResult:
        """Start QR code authentication for a Telegram account.

        This method:
        1. Creates a Telethon client with the provided API credentials
        2. Generates a QR login code
        3. Renders the QR code as a PNG image
        4. Returns the image bytes and the login object for monitoring

        The caller should:
        - Send the QR code image to the user
        - Call wait_qr_auth to wait for scan completion

        Args:
            account_id: The TelegramAccount row to authenticate.
            user_id: Must own account_id (tenant isolation).
            api_id: Telegram API ID from my.telegram.org.
            api_hash: Telegram API hash from my.telegram.org.

        Returns:
            QRAuthResult with QR code PNG bytes and login object.

        Raises:
            AccountNotFoundError: If account does not belong to user_id.
            AuthError: If QR generation fails.
        """
        account = await self._get_account_for_user(account_id, user_id)

        # Check if user already has an active account
        existing_accounts = await self._repo.list_by_user(user_id)
        for acc in existing_accounts:
            if acc.id != account_id and acc.status == TelegramAccountStatus.ACTIVE:
                raise AuthError(
                    "You already have an active account. Please disconnect it first using /disconnect."
                )

        client = self._get_client(api_id=api_id, api_hash=api_hash)
        try:
            await client.connect()
            login = await client.qr_login()

            # Generate QR code image
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(login.url)
            qr.make(fit=True)

            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format="PNG")
            qr_bytes = buffer.getvalue()

        except Exception as exc:
            raise AuthError(f"Failed to generate QR code: {exc}") from exc
        finally:
            await client.disconnect()

        return QRAuthResult(
            account_id=account_id,
            qr_code_bytes=qr_bytes,
            login_obj=login,
        )

    async def wait_qr_auth(
        self,
        account_id: int,
        user_id: int,
        login_obj: object,
        api_id: int,
        api_hash: str,
    ) -> TelegramAccount:
        """Wait for QR code scan and complete authentication.

        This method blocks until the user scans the QR code and approves the login.

        Args:
            account_id: The TelegramAccount row being authenticated.
            user_id: Must own account_id.
            login_obj: The QRLogin object from start_qr_auth.
            api_id: Telegram API ID used for the client.
            api_hash: Telegram API hash used for the client.

        Returns:
            The updated TelegramAccount (status=ACTIVE, session stored).

        Raises:
            AccountNotFoundError: Tenant violation.
            AuthError: Login fails or times out.
        """
        account = await self._get_account_for_user(account_id, user_id)

        client = self._get_client(api_id=api_id, api_hash=api_hash)
        try:
            await client.connect()

            # Wait for user to scan QR code (timeout: 5 minutes)
            try:
                await login_obj.wait(timeout=300)
            except Exception as exc:
                raise AuthError(f"QR login timed out or failed: {exc}") from exc

            # Get user info and encrypt session
            me = await client.get_me()
            cipher = SessionCipher.from_settings()
            ciphertext = cipher.encrypt(client.session.save())

        except (AccountNotFoundError, AuthError):
            raise
        except Exception as exc:
            raise AuthError(f"Authentication failed unexpectedly: {exc}") from exc
        finally:
            await client.disconnect()

        # Persist encrypted session with API credentials
        account = await self._repo.update_session(
            account,
            session_ciphertext=ciphertext,
            telegram_user_id=me.id if me else None,
            username=me.username if me else None,
            api_id=api_id,
            api_hash=api_hash,
        )
        return account
