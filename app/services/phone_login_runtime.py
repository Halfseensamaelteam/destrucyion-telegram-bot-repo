"""In-memory Telethon runtime for browser phone login."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from telethon import TelegramClient
from telethon.sessions import StringSession

from app.core.crypto import SessionCipher
from app.db.session import _get_session_factory
from app.services.account import AccountService, AuthError

PHONE_LOGIN_TIMEOUT_SECONDS = 10 * 60
MAX_LOGIN_ATTEMPTS = 3

logger = logging.getLogger(__name__)


@dataclass
class PhoneLoginRuntime:
    client: TelegramClient
    phone: str
    account_id: int
    user_id: int
    created_at: float
    stage: str = "code"
    code_future: asyncio.Future[str] | None = None
    password_future: asyncio.Future[str] | None = None
    task: asyncio.Task[None] | None = None
    error: str | None = None
    submit_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class PhoneLoginRuntimeManager:
    def __init__(self) -> None:
        self._items: dict[str, PhoneLoginRuntime] = {}
        self._lock = asyncio.Lock()

    async def create(
        self, token: str, *, user_id: int, account_id: int,
        api_id: int, api_hash: str, phone: str
    ) -> PhoneLoginRuntime:
        client = TelegramClient(StringSession(), api_id, api_hash)
        item = PhoneLoginRuntime(
            client=client, phone=phone, account_id=account_id,
            user_id=user_id, created_at=time.monotonic(),
            code_future=asyncio.get_running_loop().create_future()
        )
        async with self._lock:
            old = self._items.pop(token, None)
            if old:
                await self._stop(old)
            self._items[token] = item
        item.task = asyncio.create_task(self._run(token, item))
        return item

    async def _run(self, token: str, item: PhoneLoginRuntime) -> None:
        loop = asyncio.get_running_loop()

        async def code_callback():
            item.stage = "code"
            if item.code_future is None or item.code_future.done():
                item.code_future = loop.create_future()
            return await item.code_future

        async def password_callback():
            item.stage = "2fa"
            item.password_future = loop.create_future()
            return await item.password_future

        try:
            await item.client.start(
                phone=item.phone,
                code_callback=code_callback,
                password=password_callback,
                max_attempts=MAX_LOGIN_ATTEMPTS,
            )
            item.stage = "complete"
            await self._persist(item)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Phone login runtime failed")
            item.error = "Telegram login could not be completed. Please try again."
        finally:
            await item.client.disconnect()

    async def _persist(self, item: PhoneLoginRuntime) -> None:
        me = await item.client.get_me()
        cipher = SessionCipher.from_settings()
        session_ciphertext = cipher.encrypt(item.client.session.save())
        phone_ciphertext = cipher.encrypt(me.phone) if me and me.phone else None
        phone_masked = None
        if me and me.phone:
            phone = me.phone
            phone_masked = phone[:3] + "*" * max(0, len(phone) - 6) + phone[-3:]
        async with _get_session_factory() as session:
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

    async def get(self, token: str) -> PhoneLoginRuntime:
        async with self._lock:
            item = self._items.get(token)
        if item is None:
            raise AuthError("Phone login is missing or expired.")
        if time.monotonic() - item.created_at > PHONE_LOGIN_TIMEOUT_SECONDS:
            await self.remove(token)
            raise AuthError("Phone login expired. Please start /connect again.")
        if item.error:
            await self.remove(token)
            raise AuthError(item.error)
        return item

    async def submit_code(self, token: str, code: str) -> None:
        item = await self.get(token)
        async with item.submit_lock:
            if item.stage != "code" or item.code_future is None:
                raise AuthError("Telegram is not waiting for a login code.")
            if item.code_future.done():
                raise AuthError("This login-code request was already submitted.")
            item.code_future.set_result(code)

    async def submit_password(self, token: str, value: str) -> None:
        item = await self.get(token)
        async with item.submit_lock:
            if item.stage != "2fa" or item.password_future is None:
                raise AuthError("Telegram is not waiting for 2FA.")
            if item.password_future.done():
                raise AuthError("This 2FA request was already submitted.")
            item.password_future.set_result(value)

    async def wait_for_result(self, token: str) -> str:
        item = await self.get(token)
        if item.task is not None:
            try:
                await asyncio.wait_for(
                    asyncio.shield(item.task),
                    timeout=PHONE_LOGIN_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                await self.remove(token)
                raise AuthError("Phone login expired.")
        if item.error:
            raise AuthError(item.error)
        result = item.stage
        await self.remove(token)
        return result

    async def remove(self, token: str) -> None:
        async with self._lock:
            item = self._items.pop(token, None)
        if item:
            await self._stop(item)

    async def _stop(self, item: PhoneLoginRuntime) -> None:
        if item.task and not item.task.done():
            item.task.cancel()
        if item.client.is_connected():
            await item.client.disconnect()


phone_login_runtime = PhoneLoginRuntimeManager()
