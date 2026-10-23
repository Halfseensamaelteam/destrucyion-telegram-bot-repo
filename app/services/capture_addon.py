"""
app.services.capture_addon
~~~~~~~~~~~~~~~~~~~~~~~~~~
Business logic for the Auto-Capture add-on (/capture).

The add-on is a SEPARATE paid product from the base subscription:

  * Entitlement (CaptureSubscription) mirrors SubscriptionService's
    semantics — an active add-on extends from its existing expires_at,
    an expired/cancelled one restarts from now. Durations: weekly 7d,
    monthly 30d, quarterly 90d (no lifetime tier exists for the add-on).
  * Registration (CaptureChannel): ONE channel/group per connected
    account whose protected (noforwards) media may be auto-captured.
    Membership is verified through the account's OWN Telegram session
    before a registration is accepted.
  * CaptureGate: the worker-side check used by app.telegram.events for
    protected non-timed messages only. The bot and the worker are
    separate processes, so the gate reads the DB — behind a ~60s TTL
    cache keyed by account_id so busy channels don't add a query per
    message. Timed media and explicit saves never consult the gate.

Sessions are credentials (AGENTS.md rule 5): the membership check builds
a short-lived Telethon client from the account's stored session exactly
like AccountService.try_resume_session does, uses it for two read calls,
and disconnects. Session material is never logged or returned.
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.capture import CaptureChannel, CaptureSubscription
from app.db.models.subscription import SubscriptionPlan, SubscriptionStatus
from app.db.models.telegram_account import TelegramAccount
from app.db.repositories.capture_channel_repo import CaptureChannelRepository
from app.db.repositories.capture_subscription_repo import (
    CaptureSubscriptionRepository,
)
from app.db.repositories.telegram_account_repo import TelegramAccountRepository

log = get_logger(__name__)


class CaptureAddonError(Exception):
    """Base exception for capture add-on errors."""


class CaptureAddonService:
    """Service layer for capture add-on entitlement + registration."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._sub_repo = CaptureSubscriptionRepository(session)
        self._channel_repo = CaptureChannelRepository(session)

    # --------------------------------------------------------------
    # Entitlement
    # --------------------------------------------------------------
    @staticmethod
    def _calculate_expiration(
        plan: SubscriptionPlan, start_date: datetime
    ) -> datetime:
        """Add-on durations mirror the base plans (7 / 30 / 90 days)."""
        if plan == SubscriptionPlan.WEEKLY:
            return start_date + timedelta(days=7)
        if plan == SubscriptionPlan.MONTHLY:
            return start_date + timedelta(days=30)
        if plan == SubscriptionPlan.QUARTERLY:
            return start_date + timedelta(days=90)
        raise ValueError(f"Unsupported capture add-on plan: {plan}")

    async def get_capture_subscription(
        self, user_id: int
    ) -> CaptureSubscription | None:
        """Fetch a user's capture add-on record."""
        return await self._sub_repo.get_by_user_id(user_id)

    async def check_active(self, user_id: int) -> bool:
        """Return True if the user has a currently active capture add-on."""
        return await self._sub_repo.is_active(user_id)

    async def grant_capture_addon(
        self,
        user_id: int,
        plan: SubscriptionPlan,
        order_id: str | None = None,
        starts_at: datetime | None = None,
    ) -> CaptureSubscription:
        """Grant or extend the capture add-on for a user.

        Mirrors SubscriptionService.grant_subscription: an active add-on
        extends from its existing expiry; an expired/cancelled one
        restarts from now.
        """
        now = datetime.now(timezone.utc)
        requested_start = starts_at or now

        existing = await self._sub_repo.get_by_user_id(user_id)

        if existing is not None:
            existing_expires_at = existing.expires_at
            if existing_expires_at is not None and existing_expires_at.tzinfo is None:
                existing_expires_at = existing_expires_at.replace(
                    tzinfo=timezone.utc
                )

            if existing.is_active and existing_expires_at is not None:
                new_expires_at = self._calculate_expiration(
                    plan, existing_expires_at
                )
            else:
                new_expires_at = self._calculate_expiration(
                    plan, requested_start
                )

            existing.starts_at = (
                existing.starts_at if existing.is_active else requested_start
            )

            return await self._sub_repo.renew(
                existing,
                new_expires_at=new_expires_at,
                plan=plan,
                last_order_id=order_id,
            )

        expires_at = self._calculate_expiration(plan, requested_start)

        return await self._sub_repo.create(
            user_id=user_id,
            plan=plan,
            starts_at=requested_start,
            expires_at=expires_at,
            status=SubscriptionStatus.ACTIVE,
            last_order_id=order_id,
        )

    # --------------------------------------------------------------
    # Channel registration (1 per account, DB-enforced)
    # --------------------------------------------------------------
    async def get_registered_channel(
        self, telegram_account_id: int
    ) -> CaptureChannel | None:
        return await self._channel_repo.get_by_account_id(telegram_account_id)

    async def register_channel(
        self, telegram_account_id: int, chat_id: int
    ) -> CaptureChannel:
        """Register the account's ONE capture channel.

        Raises CaptureAddonError if a channel is already registered —
        the caller (/capture add) checks first to show a friendly
        message; the DB UNIQUE constraint is the backstop.
        """
        existing = await self._channel_repo.get_by_account_id(telegram_account_id)
        if existing is not None:
            raise CaptureAddonError(
                f"Account {telegram_account_id} already has a registered "
                f"capture channel ({existing.chat_id})."
            )
        return await self._channel_repo.create(
            CaptureChannel(
                telegram_account_id=telegram_account_id, chat_id=chat_id
            )
        )

    async def remove_channel(self, telegram_account_id: int) -> bool:
        """Delete the account's registration. Returns False if none existed."""
        return await self._channel_repo.delete_for_account(telegram_account_id)

    # --------------------------------------------------------------
    # Membership verification (bot process)
    # --------------------------------------------------------------
    async def verify_membership(
        self, account: TelegramAccount, chat_id: int
    ) -> tuple[bool, str | None]:
        """Verify the connected account is a member of chat_id.

        Builds a short-lived Telethon client from the account's stored
        session (same pattern as AccountService.try_resume_session),
        resolves the chat, requires a channel/group entity, and asks
        Telegram for the account's own participant record — which fails
        for non-members, kicked users, and unresolvable chats.

        Returns (is_member, chat_title_or_None). Never raises; any
        failure simply means "not verified".
        """
        # Imported lazily: keeps this module importable without Telethon
        # session machinery and mirrors the deferred-import style used
        # across the bot handlers.
        from telethon import TelegramClient
        from telethon.tl.types import Channel, Chat

        from app.services.account import AccountService

        account_service = AccountService(self._session)
        try:
            session = await account_service.load_session(account.id, account.user_id)
            api_id, api_hash = account_service.get_account_api_credentials(account)
        except Exception:
            return False, None

        client = TelegramClient(session, api_id, api_hash)
        try:
            await client.connect()
            if not await client.is_user_authorized():
                return False, None
            entity = await client.get_entity(chat_id)
            if not isinstance(entity, (Channel, Chat)):
                # A user id (or anything that is not a channel/group).
                return False, None
            # Raises (UserNotParticipantError et al.) when the account
            # is not a participant of the chat.
            await client.get_permissions(entity, "me")
            return True, getattr(entity, "title", None)
        except Exception:
            return False, None
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass


class CaptureGate:
    """Worker-side gate for protected (noforwards) auto-capture.

    Consulted ONLY for protected non-timed messages in automatic mode.
    A protected message may be captured only when ALL of these hold:

      * the account has a registered capture channel,
      * the message's chat_id equals that registered channel, and
      * the account owner's capture add-on is active right now.

    Results are cached per account_id for ~60s: (registered chat_id,
    add-on active) fully determines the answer for any chat_id, so a
    busy channel costs at most one DB round-trip per minute per account.
    """

    TTL_SECONDS = 60.0

    def __init__(
        self, session_factory: Callable[[], AsyncSession]
    ) -> None:
        self._session_factory = session_factory
        # account_id -> (monotonic deadline, registered chat_id | None, entitled)
        self._cache: dict[int, tuple[float, int | None, bool]] = {}

    async def is_allowed(self, *, account_id: int, chat_id: int) -> bool:
        now = time.monotonic()
        cached = self._cache.get(account_id)
        if cached is not None and cached[0] > now:
            _, registered_chat_id, entitled = cached
            return entitled and registered_chat_id == chat_id

        registered_chat_id: int | None = None
        entitled = False
        try:
            async with self._session_factory() as session:
                account = await TelegramAccountRepository(session).get_by_id(
                    account_id
                )
                if account is not None:
                    registration = await CaptureChannelRepository(
                        session
                    ).get_by_account_id(account_id)
                    if registration is not None:
                        registered_chat_id = registration.chat_id
                    entitled = await CaptureSubscriptionRepository(
                        session
                    ).is_active(account.user_id)
        except Exception as exc:
            # Fail closed (skip capture) but do not cache failures long —
            # the caller treats any exception as "not allowed" too.
            log.warning(
                "capture_gate_lookup_failed",
                account_id=account_id,
                error=str(exc),
            )
            return False

        self._cache[account_id] = (
            now + self.TTL_SECONDS,
            registered_chat_id,
            entitled,
        )
        return entitled and registered_chat_id == chat_id
