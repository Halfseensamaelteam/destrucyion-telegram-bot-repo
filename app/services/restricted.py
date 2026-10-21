"""
app.services.restricted
~~~~~~~~~~~~~~~~~~~~~~~
RestrictedContentService — fetch posts by link from chats whose content
cannot be forwarded ("Restrict saving content" / protected chats), and
save them into the account owner's own Saved Messages.

Why forwarding can never work here, and what works instead
-----------------------------------------------------------
A protected chat sets ``noforwards`` on its messages. Telegram's servers
refuse ``forwardMessages`` / ``copy`` for them — for Bot API *and* MTProto
clients alike (``ChatForwardsRestrictedError``). That restriction is a
*forwarding* rule, not an encryption: a user account that is a member of
the chat can still download the media bytes over MTProto exactly like it
downloads any other media. So the only reliable save path is:

    get_messages(peer, id)  ->  download_media(bytes)  ->  send_file(new)

That is the same download-and-re-upload path SavedMessagesService already
uses for timed media (where forwarding is equally impossible), and the
same idea the single-file reference bots use. The differences here:

  - Multi-tenant: the download runs on THE REQUESTING USER'S OWN connected
    Telethon client (worker.supervisor.TelegramClientManager). No shared
    string session, no second login flow.
  - Delivery is a userbot upload to Saved Messages, NOT a Bot API send —
    so it is not subject to the Bot API's 50 MB bot-upload limit that caps
    the reference bots.
  - Every saved item still goes through MediaCaptureService, so the DB
    UNIQUE idempotency key (account, chat, message) applies to link saves
    exactly like automatic captures. Asking for the same post twice never
    creates a duplicate copy.
  - PostgreSQL stays authoritative: bot-submitted links are stored as
    LinkRequest rows (pending) and this worker-side processor drains them;
    no Redis/in-memory queue is the source of truth.

Honest limits (CLAUDE.md §8 — never over-promise):
  - The connected account MUST be a member of a private chat. There is no
    way around that: a ``/c/`` peer cannot even be resolved from outside.
    For chats it has not joined, send the invite link first (``.get
    https://t.me/+...`` in Saved Messages, or /save with the invite link).
  - Albums (grouped media) are saved per message id; Telegram may return
    one item of an album per id, same as the reference implementation.
  - Very large batches are capped (settings.restricted_max_batch) and
    paced (settings.restricted_batch_delay_seconds) to avoid FloodWait.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from app.core.config import get_settings
from app.core.logging import get_logger
from app.telegram.link_parser import InviteRef, PostRef

log = get_logger(__name__)


class RestrictedContentError(Exception):
    """Raised when a restricted-content fetch cannot be completed."""


class NotMemberError(RestrictedContentError):
    """The connected account is not a member of the target chat."""


@dataclass
class FetchOutcome:
    """Per-message result of a link fetch (for owner-facing summaries)."""

    message_id: int
    outcome: str  # saved | duplicate | failed | not_found | text_saved
    detail: str | None = None


@dataclass
class BatchOutcome:
    """Aggregate result for one PostRef (one or more message ids)."""

    results: list[FetchOutcome] = field(default_factory=list)

    @property
    def saved_count(self) -> int:
        return sum(1 for r in self.results if r.outcome in ("saved", "text_saved"))

    @property
    def failed_count(self) -> int:
        return sum(1 for r in self.results if r.outcome in ("failed", "not_found"))


async def join_by_invite(client, invite_hash: str) -> str:
    """Join a private chat from an invite hash using THIS account's client.

    Returns a short human-readable result ("joined", "already a member").
    Raises RestrictedContentError with a user-safe message on failure
    (expired/revoked invite, etc.).
    """
    from telethon.errors import InviteHashExpiredError, UserAlreadyParticipantError
    from telethon.tl.functions.messages import ImportChatInviteRequest

    try:
        updates = await client(ImportChatInviteRequest(invite_hash))
        # ImportChatInvite returns the joined chat(s) when available.
        chats = getattr(updates, "chats", None) or []
        title = getattr(chats[0], "title", None) if chats else None
        return f"joined{': ' + title if title else ''}"
    except UserAlreadyParticipantError:
        return "already a member"
    except InviteHashExpiredError as exc:
        raise RestrictedContentError(
            "That invite link is expired or revoked. Ask for a fresh one."
        ) from exc
    except Exception as exc:  # noqa: BLE001 — surfaced to the owner verbatim-ish
        raise RestrictedContentError(f"Could not join via invite: {exc}") from exc


async def copy_text_to_saved(client, message, *, header: str) -> int:
    """Copy a text-only protected post into Saved Messages as a new message.

    Protected text cannot be forwarded either, so it is re-sent as plain
    text (the reference bots do the same). Returns the new message id.
    """
    from telethon.tl.types import InputPeerSelf

    text = getattr(message, "text", None) or getattr(message, "message", None) or ""
    sent = await client.send_message(
        entity=InputPeerSelf(),
        message=f"{header}\n\n{text}" if header else text,
    )
    return sent.id


class RestrictedContentService:
    """Fetch messages by PostRef on one account's client.

    One instance per (client, account) — never shared across accounts
    (CLAUDE.md §9 routing invariant, same as SavedMessagesService).
    """

    def __init__(self, client, account_id: int) -> None:
        self._client = client
        self._account_id = account_id

    async def fetch_messages(self, ref: PostRef) -> list:
        """Fetch the referenced messages; returns a list aligned-ish with
        ``ref.message_ids`` containing Message objects or None (deleted /
        not visible). Batch ranges are paced by the caller, not here —
        this method fetches one id per RPC so a FloodWait on one id does
        not lose the rest of the batch.
        """
        messages = []
        for msg_id in ref.message_ids:
            try:
                msg = await self._client.get_messages(ref.peer, ids=msg_id)
            except Exception as exc:
                raise self._translate_fetch_error(exc, ref) from exc
            # get_messages(ids=...) returns a single Message (or a
            # MessageEmpty / None when the id does not exist for us).
            if msg is not None and getattr(msg, "id", None) is None:
                msg = None
            messages.append(msg)
        return messages

    def _translate_fetch_error(self, exc: Exception, ref: PostRef) -> RestrictedContentError:
        name = type(exc).__name__
        if name in ("ChannelPrivateError", "ChatIdInvalidError", "PeerIdInvalidError"):
            return NotMemberError(
                "Your connected account is not a member of that chat (or the "
                "link is wrong). Join it first — send the invite link with "
                "the same command — then try the post link again."
            )
        if name == "UsernameNotOccupiedError" or name == "UsernameInvalidError":
            return RestrictedContentError("That username does not exist.")
        return RestrictedContentError(f"Could not fetch that post: {exc}")


# ----------------------------------------------------------------------
# Worker-side processor for bot-submitted LinkRequest rows
# ----------------------------------------------------------------------

# Injected by app.telegram.events to avoid a circular import: events owns
# _process_message (record + forward + admin notify); this module owns
# link fetching. Signature: (message, account_id, manager, session_factory)
# -> outcome string.
ProcessMessageFn = Callable[..., Awaitable[str]]


class LinkRequestProcessor:
    """Drains pending LinkRequest rows using each owner's own client.

    Runs inside the worker process (the only place Telethon clients live).
    The bot process only ever WRITES rows; it never touches a session.

    Polling mirrors TelegramClientManager.sync_accounts: PostgreSQL is
    authoritative, the loop simply re-reads it on an interval.
    """

    def __init__(
        self,
        session_factory,
        manager,
        *,
        process_message: ProcessMessageFn,
        poll_interval: float | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._manager = manager
        self._process_message = process_message
        if poll_interval is None:
            from app.core.config import get_settings

            poll_interval = get_settings().link_request_poll_seconds
        self._poll_interval = poll_interval

    async def run_forever(self) -> None:
        """Poll until cancelled (worker shutdown cancels this task)."""
        log.info("link_request_processor_started", interval=self._poll_interval)
        # A previous run may have died mid-request, leaving rows stuck in
        # PROCESSING forever — return them to the queue before polling.
        try:
            from app.db.repositories.link_request_repo import LinkRequestRepository

            async with self._session_factory() as session:
                repo = LinkRequestRepository(session)
                reset = await repo.reset_processing_to_pending()
                if reset:
                    await session.commit()
                    log.info("link_requests_stale_reset", count=reset)
        except Exception as exc:  # never block startup on the reset
            log.warning("link_requests_stale_reset_failed", error=str(exc))
        while True:
            try:
                await self.process_pending_once()
            except Exception as exc:  # one bad cycle must never kill the loop
                log.error("link_request_cycle_failed", error=str(exc))
            await asyncio.sleep(self._poll_interval)

    async def process_pending_once(self) -> int:
        """Process every currently-pending request once. Returns count."""
        from app.db.repositories.link_request_repo import LinkRequestRepository

        async with self._session_factory() as session:
            repo = LinkRequestRepository(session)
            pending = await repo.list_pending(limit=50)

        for request in pending:
            await self._process_request(request)
        return len(pending)

    async def _process_request(self, request) -> None:
        from app.db.models.link_request import LinkRequestStatus
        from app.db.repositories.link_request_repo import LinkRequestRepository
        from app.telegram.link_parser import parse_link

        settings = get_settings()

        async def _set_status(status, *, result_count=None, error=None):
            async with self._session_factory() as session:
                repo = LinkRequestRepository(session)
                fresh = await repo.get_by_id(request.id)
                if fresh is None:
                    return
                await repo.mark(fresh, status=status, result_count=result_count, error=error)
                await session.commit()

        await _set_status(LinkRequestStatus.PROCESSING)

        try:
            client = self._manager.get_client(request.telegram_account_id)
            if client is None:
                raise RestrictedContentError(
                    "Your Telegram account is not running in the worker "
                    "(disconnected or subscription inactive)."
                )

            parsed = parse_link(request.raw_link)
            if parsed is None:
                raise RestrictedContentError("That link could not be understood.")

            if isinstance(parsed, InviteRef):
                result = await join_by_invite(client, parsed.invite_hash)
                await _set_status(LinkRequestStatus.DONE, result_count=0)
                await self._notify_owner(
                    request, f"✅ Invite handled ({result}). Now send the post link."
                )
                return

            # Tell the owner the moment work actually starts — a large
            # video download+upload can take minutes, and silence reads
            # exactly like failure.
            await self._notify_owner(
                request,
                "⏳ Working on it — downloading now…",
            )
            saved = await self._save_ref(client, request, parsed, settings)
            await _set_status(LinkRequestStatus.DONE, result_count=saved)
            await self._notify_owner(
                request,
                f"✅ Done: {saved} item(s) from your link are in your Saved Messages.",
            )
        except Exception as exc:
            log.warning(
                "link_request_failed",
                request_id=request.id,
                account_id=request.telegram_account_id,
                error=str(exc),
            )
            await _set_status(LinkRequestStatus.FAILED, error=str(exc)[:2000])
            await self._notify_owner(request, f"❌ Link failed: {exc}")

    async def _save_ref(self, client, request, ref: PostRef, settings) -> int:
        service = RestrictedContentService(client, account_id=request.telegram_account_id)
        ids = ref.message_ids[: settings.restricted_max_batch]
        saved = 0
        for index, msg_id in enumerate(ids):
            single = PostRef(
                peer=ref.peer, message_ids=(msg_id,),
                is_private=ref.is_private, topic_id=ref.topic_id,
            )
            (message,) = await service.fetch_messages(single)
            if message is None:
                continue
            outcome = await self._process_message(
                message=message,
                account_id=request.telegram_account_id,
                manager=self._manager,
                session_factory=self._session_factory,
                force=True,
                capture_only_timed=False,
            )
            if outcome in ("saved", "duplicate"):
                saved += 1
            if index < len(ids) - 1 and settings.restricted_batch_delay_seconds > 0:
                await asyncio.sleep(settings.restricted_batch_delay_seconds)
        return saved

    async def _notify_owner(self, request, text: str) -> None:
        """Best-effort Bot API status update for the requesting user.

        One status message per request: when the queue row carries the
        bot's status-message ids, the message is EDITED in place
        (Queued -> Working -> Done/Failed); rows without ids (queued
        before this existed) fall back to a plain new message. The saved
        content itself lands in the owner's Saved Messages via their own
        client — this is only the courtesy status in the bot chat, and
        failure here never changes the request outcome.
        """
        try:
            from app.db.repositories.user_repo import UserRepository
            from app.services.telegram_notifier import TelegramNotifier

            notifier = TelegramNotifier()
            chat_id = getattr(request, "notify_chat_id", None)
            message_id = getattr(request, "notify_message_id", None)
            if chat_id and message_id:
                try:
                    await notifier.edit_text(
                        chat_id=chat_id, message_id=message_id, text=text
                    )
                    return
                except Exception as exc:
                    # Deleted message / too old to edit — fall through to
                    # a fresh message so the owner still hears the result.
                    log.debug(
                        "link_request_status_edit_failed",
                        request_id=request.id,
                        error=str(exc),
                    )
            async with self._session_factory() as session:
                user = await UserRepository(session).get_by_id(request.user_id)
            if user is None:
                return
            await notifier.send_text(telegram_user_id=user.telegram_user_id, text=text)
        except Exception as exc:
            log.debug("link_request_notify_failed", request_id=request.id, error=str(exc))
