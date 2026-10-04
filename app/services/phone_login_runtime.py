"""In-memory Telethon coordinator for browser-based phone login.

Redis stores only short-lived browser state. The connected Telethon client stays
in this process for the entire send-code -> OTP -> optional 2FA sequence.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from telethon import TelegramClient
from telethon.errors import RPCError, SessionPasswordNeededError
from telethon.sessions import StringSession

from app.core.crypto import SessionCipher
from app.db.session import _get_session_factory
from app.services.account import AccountService, AuthError

PHONE_LOGIN_TIMEOUT_SECONDS = 10 * 60
MAX_CODE_ATTEMPTS = 5
MAX_2FA_ATTEMPTS = 3


@dataclass
class PhoneLoginRuntime:
    client: TelegramClient
    phone: str
    phone_code_hash: str
    account_id: int
    user_id: int
    api_id: int
    api_hash: str
    created_at: float
    code_attempts: int = 0
    twofa_attempts: int = 0
    stage: str = "code"


class PhoneLoginRuntimeManager:
    def __init__(self) -> None:
        self._items: dict[str, PhoneLoginRuntime] = {}
        self._lock = asyncio.Lock()

    async def create(self, token: str, *, user_id: int, account_id: int, api_id: int, api_hash: str, phone: str) -> PhoneLoginRuntime:
        if not token:
            raise AuthError("Invalid phone login ticket.")
        client = TelegramClient(StringSession(), api_id, api_hash)
        try:
            await client.connect()
            result = await client.send_code_request(phone)
        except Exception as exc:
            await client.disconnect()
            raise AuthError(f"Could not send Telegram login code: {exc}") from exc
        item = PhoneLoginRuntime(
            client=client,
            phone=phone,
            phone_code_hash=result.phone_code_hash,
            account_id=account_id,
            user_id=user_id,
            api_id=api_id,
            api_hash=api_hash,
            created_at=time.monotonic(),
        )
        async with self._lock:
            old = self._items.pop(token, None)
            if old:
                await old.client.disconnect()
            self._items[token] = item
        return item

    async def get(self, token: str) -> PhoneLoginRuntime:
        async with self._lock:
            item = self._items.get(token)
        if not item or time.monotonic() - item.created_at > PHONE_LOGIN_TIMEOUT_SECONDS:
            if item:
                await self.remove(token)
            raise AuthError("Phone login expired. Please start /connect again.")
        return item

    async def verify_code(self, token: str, code: str) -> tuple[bool, bool]:
        item = await self.get(token)
        if item.code_attempts >= MAX_CODE_ATTEMPTS:
            await self.remove(token)
            raise AuthError("Too many OTP attempts. Please start again.")
        item.code_attempts += 1
        try:
            await item.client.sign_in(
                phone=item.phone,
                code=code,
                phone_code_hash=item.phone_code_hash,
            )
            return True, False
        except SessionPasswordNeededError:
            item.stage = "2fa"
            return False, True
        except RPCError as exc:
            if item.code_attempts >= MAX_CODE_ATTEMPTS:
                await self.remove(token)
            raise AuthError(f"Telegram rejected the login code: {exc}") from exc

    async def verify_2fa(self, token: str, password: str) -> bool:
        item = await self.get(token)
        if item.stage != "2fa":
            raise AuthError("2FA is not required for this login.")
        if item.twofa_attempts >= MAX_2FA_ATTEMPTS:
            await self.remove(token)
            raise AuthError("Too many 2FA attempts. Please start again.")
        item.twofa_attempts += 1
        try:
            await item.client.sign_in(password=password)
            return True
        except RPCError as exc:
            if item.twofa_attempts >= MAX_2FA_ATTEMPTS:
                await self.remove(token)
            raise AuthError(f"Telegram rejected the 2FA password: {exc}") from exc

    async def finish(self, token: str) -> None:
        item = await self.get(token)
        if not await item.client.is_user_authorized():
            raise AuthError("Telegram login is not authorized.")
        me = await item.client.get_me()
        cipher = SessionCipher.from_settings()
        session_ciphertext = cipher.encrypt(item.client.session.save())
        phone_ciphertext = cipher.encrypt(me.phone) if me and me.phone else None
        phone_masked = None
        if me and me.phone:
            phone = me.phone
            phone_masked = phone[:3] + "*" * max(0, len(phone) - 6) + phone[-3:]
        session_factory = _get_session_factory()
        async with session_factory() as session:
            svc = AccountService(session)
            account = await svc._get_account_for_user(item.account_id, item.user_id)
            await svc._repo.update_session(
                account,
                session_ciphertext=session_ciphertext,
                telegram_user_id=me.id if me else None,
                username=me.username if me else None,
                phone_masked=phone_masked,
                phone_ciphertext=phone_ciphertext,
            )
            await session.commit()
        await self.remove(token)

    async def remove(self, token: str) -> None:
        async with self._lock:
            item = self._items.pop(token, None)
        if item:
            await item.client.disconnect()


phone_login_runtime = PhoneLoginRuntimeManager()
