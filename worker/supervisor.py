"""
worker.supervisor
~~~~~~~~~~~~~~~~~
TelegramClientManager — manages isolated Telethon clients per Telegram account.

Design rules (CLAUDE.md §9):
  - One TelegramClient per Telegram account. NEVER shared.
  - Account B failure must NOT affect Account A, C, D.
  - Sessions decrypted here only — never logged, never returned.
  - Subscription checked before starting each account's client.

State machine per account:
  STOPPED → STARTING → RUNNING → STOPPING → STOPPED
                                          ↘ RECONNECTING → RUNNING
                                          ↘ ERROR
"""

from __future__ import annotations

import asyncio
from enum import Enum, auto
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Awaitable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.core.crypto import SessionCipher, SessionEncryptionError
from app.db.models.telegram_account import TelegramAccount, TelegramAccountStatus
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.subscription import SubscriptionService

log = get_logger(__name__)

# Maximum number of reconnect attempts before marking account as ERROR
MAX_RECONNECT_ATTEMPTS = 5
# Base delay for exponential backoff (seconds)
RECONNECT_BASE_DELAY = 5.0


class ClientState(Enum):
    STOPPED = auto()
    STARTING = auto()
    RUNNING = auto()
    STOPPING = auto()
    RECONNECTING = auto()
    ERROR = auto()


@dataclass
class ManagedClient:
    """Holds state for a single managed Telethon account."""

    account_id: int
    user_id: int
    state: ClientState = ClientState.STOPPED
    client: object = None  # TelegramClient instance
    reconnect_attempts: int = 0
    last_error: str | None = None
    started_at: datetime | None = None
    task: asyncio.Task | None = None
    lock_token: str | None = None
    heartbeat_task: asyncio.Task | None = None

    def __repr__(self) -> str:
        return (
            f"ManagedClient(account_id={self.account_id}, "
            f"user_id={self.user_id}, state={self.state.name})"
        )


# Type alias for the event handler factory (set in Phase 7)
EventHandlerFactory = Callable[["TelegramClientManager", int], list]


class TelegramClientManager:
    """Manages a pool of isolated Telethon clients, one per Telegram account.

    Each account gets its own client, event loop task, and isolated state.
    A failure in one account does not cascade to others.

    Usage in the worker:
        manager = TelegramClientManager(get_session)
        await manager.start()
        try:
            await manager.run_forever()
        finally:
            await manager.stop()
    """

    def __init__(
        self,
        session_factory: Callable[[], AsyncSession],
        *,
        cipher: SessionCipher | None = None,
        handler_factory: EventHandlerFactory | None = None,
        account_lock: object | None = None,  # AccountLock
    ) -> None:
        """
        Args:
            session_factory: Callable that returns a new AsyncSession. The manager
                             creates a fresh session per account to avoid sharing
                             database connections across coroutines.
            cipher: SessionCipher for decrypting stored sessions. If None, loads
                    from application settings (SESSION_ENCRYPTION_KEY).
            handler_factory: Optional factory to attach Telethon event handlers.
                             Will be wired in Phase 7 for media capture.
        """
        self._session_factory = session_factory
        self._cipher = cipher or SessionCipher.from_settings()
        self._handler_factory = handler_factory
        
        # Load AccountLock. Fallback to singleton if not provided.
        if account_lock is None:
            from app.core.redis import get_redis_client
            from worker.lock import AccountLock
            account_lock = AccountLock(get_redis_client())
        self._account_lock = account_lock

        self._clients: dict[int, ManagedClient] = {}  # keyed by account_id
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Load all active accounts from DB and start their Telethon clients."""
        log.info("client_manager_starting")
        accounts = await self._load_active_accounts()
        log.info("client_manager_accounts_loaded", count=len(accounts))

        for account in accounts:
            await self._start_account(account)

        log.info("client_manager_started", running=len(self._clients))

    async def stop(self) -> None:
        """Gracefully disconnect all active Telethon clients."""
        log.info("client_manager_stopping", account_count=len(self._clients))
        self._stop_event.set()

        tasks = []
        for managed in list(self._clients.values()):
            tasks.append(self._stop_account(managed))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._clients.clear()
        log.info("client_manager_stopped")

    async def run_forever(self) -> None:
        """Block until stop() is called.

        In a real deployment this keeps the worker alive. Tests can call
        stop() to exit this method.
        """
        await self._stop_event.wait()

    async def add_account(self, account: TelegramAccount) -> None:
        """Dynamically add and start a new account without restarting the manager.

        Called after a user completes Phase 5 authentication.
        """
        await self._start_account(account)

    async def remove_account(self, account_id: int) -> None:
        """Stop and remove an account from the manager."""
        async with self._lock:
            managed = self._clients.get(account_id)
        if managed:
            await self._stop_account(managed)
            async with self._lock:
                self._clients.pop(account_id, None)

    def get_state(self, account_id: int) -> ClientState | None:
        """Return the current state of an account's client."""
        managed = self._clients.get(account_id)
        return managed.state if managed else None

    def get_client(self, account_id: int):
        """Return the raw TelegramClient for an account (or None).

        Used by Phase 7 media capture to forward media to Saved Messages.
        """
        managed = self._clients.get(account_id)
        if managed and managed.state == ClientState.RUNNING:
            return managed.client
        return None

    def all_states(self) -> dict[int, str]:
        """Return a dict of account_id → state name for health monitoring."""
        return {aid: mc.state.name for aid, mc in self._clients.items()}

    # ------------------------------------------------------------------
    # Internal: account lifecycle
    # ------------------------------------------------------------------

    async def _load_active_accounts(self) -> list[TelegramAccount]:
        """Load all ACTIVE Telegram accounts from the database."""
        async with self._session_factory() as session:
            repo = TelegramAccountRepository(session)
            return await repo.list_active()

    async def _check_subscription(self, user_id: int) -> bool:
        """Return True if the user has an active subscription."""
        async with self._session_factory() as session:
            service = SubscriptionService(session)
            return await service.check_active(user_id)

    async def _start_account(self, account: TelegramAccount) -> None:
        """Decrypt session, create client, attach handlers, and launch task."""
        account_id = account.id

        # Subscription gate
        if not await self._check_subscription(account.user_id):
            log.warning(
                "client_skipped_no_subscription",
                account_id=account_id,
                user_id=account.user_id,
            )
            return

        # Distributed lock — prevent multiple workers from starting the same account
        lock_token = await self._account_lock.acquire(account_id)
        if not lock_token:
            # Another worker owns this account; skip it silently
            return

        if not account.session_ciphertext:
            log.warning(
                "client_skipped_no_session",
                account_id=account_id,
            )
            return

        # Decrypt session — NEVER log the result
        try:
            session_string = self._cipher.decrypt(account.session_ciphertext)
        except SessionEncryptionError:
            log.error(
                "client_session_decrypt_failed",
                account_id=account_id,
                # session string is NOT included in this log
            )
            await self._mark_error(account_id, "Session decryption failed.")
            return

        from app.core.config import get_settings
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        settings = get_settings()
        client = TelegramClient(
            StringSession(session_string),
            settings.telegram_api_id,
            settings.telegram_api_hash,
        )
        # session_string goes out of scope here — GC will clear it

        managed = ManagedClient(
            account_id=account_id,
            user_id=account.user_id,
            state=ClientState.STARTING,
            client=client,
            lock_token=lock_token,
        )

        # Launch heartbeat task
        managed.heartbeat_task = asyncio.create_task(
            self._account_lock.heartbeat(account_id, lock_token),
            name=f"heartbeat-{account_id}",
        )

        async with self._lock:
            self._clients[account_id] = managed

        # Attach event handlers (Phase 7 wires media capture here)
        if self._handler_factory:
            for handler in self._handler_factory(self, account_id):
                client.add_event_handler(*handler)

        # Launch isolated task — failures here don't affect other accounts
        task = asyncio.create_task(
            self._run_account(managed),
            name=f"account-{account_id}",
        )
        managed.task = task

    async def _run_account(self, managed: ManagedClient) -> None:
        """Main lifecycle coroutine for a single account's Telethon client.

        Handles connect, running, reconnect-on-disconnect, and graceful stop.
        Each account runs in its own task — isolation guaranteed.
        """
        account_id = managed.account_id

        while not self._stop_event.is_set():
            try:
                managed.state = ClientState.STARTING
                await managed.client.connect()

                if not await managed.client.is_user_authorized():
                    log.error(
                        "client_not_authorized",
                        account_id=account_id,
                    )
                    managed.state = ClientState.ERROR
                    managed.last_error = "Session expired or not authorized."
                    await self._persist_error(account_id, managed.last_error)
                    return

                managed.state = ClientState.RUNNING
                managed.reconnect_attempts = 0
                managed.started_at = datetime.now(timezone.utc)
                log.info("client_running", account_id=account_id)

                await self._persist_connected(account_id)

                # Keep alive until the client disconnects or stop is requested
                # Also abort if the heartbeat task exits (which means we lost the lock)
                client_task = asyncio.create_task(managed.client.run_until_disconnected())
                done, pending = await asyncio.wait(
                    [client_task, managed.heartbeat_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                if managed.heartbeat_task in done:
                    # Heartbeat exited -> lock was lost
                    log.error("client_lock_lost", account_id=account_id)
                    client_task.cancel()
                    break

            except asyncio.CancelledError:
                log.info("client_cancelled", account_id=account_id)
                break

            except Exception as exc:
                managed.last_error = str(exc)
                log.warning(
                    "client_disconnected",
                    account_id=account_id,
                    error=str(exc),
                    attempt=managed.reconnect_attempts,
                )

                if self._stop_event.is_set():
                    break

                managed.reconnect_attempts += 1
                if managed.reconnect_attempts > MAX_RECONNECT_ATTEMPTS:
                    log.error(
                        "client_max_reconnects_exceeded",
                        account_id=account_id,
                        max=MAX_RECONNECT_ATTEMPTS,
                    )
                    managed.state = ClientState.ERROR
                    await self._persist_error(account_id, managed.last_error)
                    return

                # Exponential backoff
                delay = RECONNECT_BASE_DELAY * (2 ** (managed.reconnect_attempts - 1))
                log.info(
                    "client_reconnecting",
                    account_id=account_id,
                    delay=delay,
                    attempt=managed.reconnect_attempts,
                )
                managed.state = ClientState.RECONNECTING
                await asyncio.sleep(delay)

            finally:
                try:
                    await managed.client.disconnect()
                except Exception:
                    pass

        managed.state = ClientState.STOPPED
        log.info("client_stopped", account_id=account_id)

    async def _stop_account(self, managed: ManagedClient) -> None:
        """Cancel a single account's task and disconnect its client."""
        account_id = managed.account_id
        managed.state = ClientState.STOPPING

        if managed.task and not managed.task.done():
            managed.task.cancel()
            try:
                await managed.task
            except (asyncio.CancelledError, Exception):
                pass

        if managed.heartbeat_task and not managed.heartbeat_task.done():
            managed.heartbeat_task.cancel()
            try:
                await managed.heartbeat_task
            except (asyncio.CancelledError, Exception):
                pass

        try:
            if managed.client:
                await managed.client.disconnect()
        except Exception:
            pass

        if managed.lock_token:
            await self._account_lock.release(account_id, managed.lock_token)
            managed.lock_token = None

        managed.state = ClientState.STOPPED
        log.info("client_disconnected_cleanly", account_id=account_id)

    async def _mark_error(self, account_id: int, error: str) -> None:
        managed = self._clients.get(account_id)
        if managed:
            managed.state = ClientState.ERROR
            managed.last_error = error
        await self._persist_error(account_id, error)

    # ------------------------------------------------------------------
    # Internal: database updates
    # ------------------------------------------------------------------

    async def _persist_connected(self, account_id: int) -> None:
        try:
            async with self._session_factory() as session:
                repo = TelegramAccountRepository(session)
                account = await repo.get_by_id(account_id)
                if account:
                    await repo.update_status(
                        account,
                        TelegramAccountStatus.ACTIVE,
                        last_connected_at=datetime.now(timezone.utc),
                    )
                    await session.commit()
        except Exception as exc:
            log.warning("persist_connected_failed", account_id=account_id, error=str(exc))

    async def _persist_error(self, account_id: int, error: str) -> None:
        try:
            async with self._session_factory() as session:
                repo = TelegramAccountRepository(session)
                account = await repo.get_by_id(account_id)
                if account:
                    await repo.update_status(
                        account,
                        TelegramAccountStatus.ERROR,
                        last_error=error,
                    )
                    await session.commit()
        except Exception as exc:
            log.warning("persist_error_failed", account_id=account_id, error=str(exc))
