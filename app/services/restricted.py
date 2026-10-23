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
from app.core.disk_lease import DiskLeaseManager
from app.core.logging import get_logger
from app.telegram.link_parser import InviteRef, PostRef
from app.telegram.media import classify_message

log = get_logger(__name__)

#: Budget-only mode's automatic temp-disk guard: each in-flight slot is
#: assumed to hold one 200 MB-class temp file plus headroom (x1.5), so
#: slots = free temp MB // this reserve. Deliberately conservative —
#: that mode sizes ADMISSION (how many slots), while the size-aware
#: disk leases (app.core.disk_lease) size the BYTES of requests that
#: have already been fetched and measured.
_DISK_RESERVE_PER_SLOT_MB = 300

_MB = 1024 * 1024


def request_download_size_mb(messages, *, unknown_size_mb: float) -> float:
    """Total temp-disk MB a fetched request will occupy, BEFORE download.

    Only download+upload writes temp bytes, and SavedMessagesService
    takes that path exactly when a post is timed (ttl) or protected
    (noforwards) — both visible on the fetched message, as is the file
    size (``message.file.size``). Everything else is a server-side
    forward: zero temp bytes, so it leases nothing. (A forward that
    unexpectedly falls back to a resend downloads WITHOUT a lease —
    rare, transient, and freed immediately after; the lease system
    deliberately does not tax the common forward path for it.)

    A download-path message whose size Telegram does not report is
    accounted at ``unknown_size_mb`` (the big-file threshold) and so
    also pushes the request into the big class — the conservative side.
    """
    total_bytes = 0
    for message in messages:
        if message is None:
            continue
        info = classify_message(message)
        if info is None:
            continue  # text-only / unsupported: nothing is downloaded
        if not (info.is_protected or info.is_self_destruct):
            continue  # server-side forward path: no temp file
        size = getattr(getattr(message, "file", None), "size", None)
        if not isinstance(size, (int, float)) or size <= 0:
            total_bytes += int(unknown_size_mb * _MB)
        else:
            total_bytes += int(size)
    return total_bytes / _MB


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
        max_concurrency: int | None = None,
        memory_budget_mb: int | None = None,
        disk_reserve_mb: int | None = None,
        disk_big_file_mb: int | None = None,
        disk_big_pool_percent: int | None = None,
        disk_big_guarantee_percent: int | None = None,
        lease_manager: DiskLeaseManager | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._manager = manager
        self._process_message = process_message
        from app.core.config import get_settings
        from app.core.resources import process_rss_mb, temp_free_mb

        settings = get_settings()
        if poll_interval is None:
            poll_interval = settings.link_request_poll_seconds
        self._poll_interval = poll_interval
        if max_concurrency is None:
            max_concurrency = settings.link_request_max_concurrency
        if memory_budget_mb is None:
            memory_budget_mb = settings.link_request_memory_budget_mb
        self._memory_budget_mb = memory_budget_mb if memory_budget_mb > 0 else None
        # Budget-only mode: a slot cap of 0 TOGETHER with a memory
        # budget removes the human-set ceiling entirely — parallelism
        # is then sized purely by measured RAM (AIMD below) plus an
        # automatic temp-disk guard, because RAM cannot see the disk:
        # hundreds of slots fit in a few GB of RAM long before their
        # temp files fit on disk. A cap of 0 WITHOUT a budget would
        # leave the queue unguarded, so it falls back to the default.
        self._budget_only = (
            max_concurrency == 0 and self._memory_budget_mb is not None
        )
        if max_concurrency == 0:
            max_concurrency = 100_000 if self._budget_only else 10
        self._max_concurrency = max(1, max_concurrency)
        self._stale_seconds = settings.link_request_stale_seconds
        self._semaphore = asyncio.Semaphore(self._max_concurrency)
        # One in-flight request per account: a Telethon client serves its
        # owner's saves one at a time (sequential per account also keeps
        # Saved Messages ordering sane), while DIFFERENT accounts proceed
        # fully in parallel up to the global cap.
        self._account_locks: dict[int, asyncio.Lock] = {}
        self._rss_reader = process_rss_mb
        self._temp_free_reader = temp_free_mb
        # Size-aware disk admission (app.core.disk_lease): after a
        # request's messages are fetched — when their total download
        # size is known but before a single byte is downloaded — the
        # request leases that many temp-disk bytes, so parallelism is
        # bounded by BYTES with a two-class pool/guarantee split, not
        # just by slot count. Reserve 0 disables it (pre-existing
        # behaviour, bit for bit). The manager reads free space through
        # this processor's reader, so tests can substitute one seam.
        if lease_manager is not None:
            self._lease_manager = lease_manager
        else:
            self._lease_manager = DiskLeaseManager(
                reserve_mb=(
                    settings.link_request_disk_reserve_mb
                    if disk_reserve_mb is None
                    else disk_reserve_mb
                ),
                big_file_mb=(
                    settings.link_request_big_file_mb
                    if disk_big_file_mb is None
                    else disk_big_file_mb
                ),
                big_pool_percent=(
                    settings.link_request_big_pool_percent
                    if disk_big_pool_percent is None
                    else disk_big_pool_percent
                ),
                big_guarantee_percent=(
                    settings.link_request_big_guarantee_percent
                    if disk_big_guarantee_percent is None
                    else disk_big_guarantee_percent
                ),
                free_mb_reader=lambda: self._temp_free_reader(),
            )
        self._last_disk_cap: int | None = None
        # The adaptive limit STARTS at the full cap: the budget is a
        # brake, not a ramp. The supervisor re-evaluates every tick
        # (seconds, and instantly on completions), so if RAM actually
        # approaches the budget the limit is halved within one tick —
        # while a workload that never threatens RAM runs at exactly
        # fixed-cap speed from the first second. (An earlier design
        # started at 10 and grew +25% per BATCH; measured on a 600-file
        # run that made budget mode 75% slower than the fixed cap,
        # because a batch lives as long as its slowest transfer.)
        self._dynamic_limit = self._max_concurrency
        self._last_claim_limit = self._max_concurrency
        # Live transfer tasks owned by the supervisor loop, by request
        # id. Admission = claim only while len(_in_flight) < limit.
        self._in_flight: dict[int, asyncio.Task] = {}
        self._last_stale_reset = 0.0

    def _effective_claim_limit(self) -> int:
        """How many requests to claim in the next batch.

        Fixed mode: always the configured cap. Adaptive mode: AIMD on
        the worker's own RSS vs the memory budget — halve the batch
        when over budget, grow it by a quarter when comfortably under
        (80%), hold steady in between or when RSS is unreadable.
        Budget-only mode additionally applies the temp-disk guard:
        free temp space divided by a per-slot reserve (a 200 MB-class
        file x 1.5), recomputed every cycle.
        """
        if self._memory_budget_mb is None:
            limit = self._max_concurrency
        else:
            rss = self._rss_reader()
            if rss is not None:
                old = self._dynamic_limit
                if rss > self._memory_budget_mb:
                    self._dynamic_limit = max(1, old // 2)
                elif rss < 0.8 * self._memory_budget_mb:
                    self._dynamic_limit = min(
                        self._max_concurrency, old + max(1, old // 4)
                    )
                if self._dynamic_limit != old:
                    log.info(
                        "link_request_concurrency_adjusted",
                        rss_mb=round(rss),
                        budget_mb=self._memory_budget_mb,
                        limit=self._dynamic_limit,
                    )
            limit = self._dynamic_limit
        if self._budget_only:
            free = self._temp_free_reader()
            if free is not None:
                disk_cap = max(1, int(free // _DISK_RESERVE_PER_SLOT_MB))
                if disk_cap != self._last_disk_cap:
                    log.info(
                        "link_request_disk_guard",
                        free_temp_mb=round(free),
                        disk_cap=disk_cap,
                    )
                    self._last_disk_cap = disk_cap
                limit = min(limit, disk_cap)
        self._last_claim_limit = limit
        return limit

    async def run_forever(self) -> None:
        """Continuous admission supervisor (until cancelled).

        Slots are refilled the moment a transfer finishes and the
        effective limit is re-evaluated every tick (at most the poll
        interval apart, and immediately on any completion). This is
        what makes the adaptive controller usable: earlier the limit
        only changed BETWEEN batches, and a batch lives as long as its
        slowest transfer — ramping 10 -> 100 took a dozen batches
        (many minutes) and most of a run crawled far below capacity.
        """
        log.info(
            "link_request_processor_started",
            interval=self._poll_interval,
            max_concurrency=self._max_concurrency,
            memory_budget_mb=self._memory_budget_mb,
            budget_only=self._budget_only,
            disk_reserve_mb=(
                self._lease_manager.reserve_mb
                if self._lease_manager.enabled
                else 0
            ),
        )
        while True:
            try:
                await self._reset_stale_throttled()
                self._reap()
                # Lease waiters normally wake on releases; the tick
                # also covers free space changed by OTHER tenants of
                # the disk (logs, another container, the operator).
                self._lease_manager.kick()
                await self._top_up()
            except Exception as exc:  # one bad cycle must never kill the loop
                log.error("link_request_cycle_failed", error=str(exc))
            if self._in_flight:
                await asyncio.wait(
                    set(self._in_flight.values()),
                    timeout=self._poll_interval,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            else:
                await asyncio.sleep(self._poll_interval)

    async def _reset_stale_throttled(self) -> None:
        import time as _time

        now = _time.monotonic()
        if now - self._last_stale_reset < 30:
            return
        self._last_stale_reset = now
        # Requeue rows whose worker died mid-request. Age-boxed: with
        # several workers running, a live peer's in-flight rows are
        # never stolen — only claims older than the threshold return.
        await self._reset_stale()

    def _reap(self) -> None:
        """Drop finished in-flight tasks, surfacing unexpected errors."""
        for rid in [r for r, t in self._in_flight.items() if t.done()]:
            task = self._in_flight.pop(rid)
            if not task.cancelled() and task.exception() is not None:
                log.error(
                    "link_request_task_failed",
                    request_id=rid,
                    error=str(task.exception()),
                )

    async def shutdown(self) -> None:
        """Cancel in-flight transfers (worker shutdown / test teardown)."""
        tasks = list(self._in_flight.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._in_flight.clear()

    async def _reset_stale(self) -> None:
        try:
            from app.db.repositories.link_request_repo import LinkRequestRepository

            async with self._session_factory() as session:
                repo = LinkRequestRepository(session)
                reset = await repo.reset_processing_to_pending(
                    older_than_seconds=self._stale_seconds
                )
                if reset:
                    await session.commit()
                    log.info("link_requests_stale_reset", count=reset)
        except Exception as exc:  # housekeeping must never block the queue
            log.warning("link_requests_stale_reset_failed", error=str(exc))

    async def _top_up(self) -> int:
        """Claim rows up to the free capacity and start them now.

        The supervisor's only admission point. Claiming (PENDING ->
        PROCESSING in a single UPDATE) is what makes several worker
        processes safe against one queue: no row can be handed to two
        workers. Capacity = effective limit - already in flight, so a
        finished transfer's slot is reused within one tick.
        """
        from app.db.repositories.link_request_repo import LinkRequestRepository

        capacity = self._effective_claim_limit() - len(self._in_flight)
        if capacity <= 0:
            return 0
        async with self._session_factory() as session:
            repo = LinkRequestRepository(session)
            claimed = await repo.claim_pending(limit=capacity)
            for row in claimed:
                # Keep loaded attributes usable after the session closes
                # (commit would otherwise expire them).
                session.expunge(row)
            await session.commit()
        for row in claimed:
            self._in_flight[row.id] = asyncio.create_task(self._run_one(row))
        return len(claimed)

    async def process_pending_once(self) -> int:
        """Top up once, then run everything in flight to completion.

        Kept for tests and one-shot drains; production runs the
        continuous supervisor in run_forever. Returns how many requests
        this call claimed.
        """
        claimed = await self._top_up()
        if self._in_flight:
            await asyncio.gather(
                *list(self._in_flight.values()), return_exceptions=True
            )
            self._reap()
        return claimed

    async def _run_one(self, request) -> None:
        async with self._semaphore:
            lock = self._account_locks.setdefault(
                request.telegram_account_id, asyncio.Lock()
            )
            async with lock:
                try:
                    await self._process_request(request)
                except Exception as exc:
                    # _process_request handles its own failures (marks
                    # the row FAILED); anything escaping to here is a
                    # bookkeeping failure — log it, never kill the batch.
                    log.error(
                        "link_request_unhandled_error",
                        request_id=request.id,
                        error=str(exc),
                    )

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
        # Fetch the whole ref first (the same per-id RPC loop, just
        # all of it up front): the request's total download size is
        # only knowable once every message has been seen, and the disk
        # lease below must be held BEFORE the first byte is downloaded.
        # Trade-off, accepted deliberately: a fetch error mid-range now
        # aborts the request before any item is saved, where the old
        # one-at-a-time loop had already saved the earlier items (the
        # request was marked FAILED either way; in practice fetch
        # errors are chat-level and hit on the first id).
        capped = PostRef(
            peer=ref.peer, message_ids=tuple(ids),
            is_private=ref.is_private, topic_id=ref.topic_id,
        )
        messages = await service.fetch_messages(capped)
        lease = await self._acquire_disk_lease(request, messages, settings)
        try:
            saved = 0
            for index, message in enumerate(messages):
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
        finally:
            if lease is not None:
                lease.release()

    async def _acquire_disk_lease(self, request, messages, settings):
        """Lease this request's total download size, or None.

        None when disk admission is disabled (reserve 0) or when the
        request downloads nothing at all (pure forwards / text) — a
        zero lease could never wait, so it is never taken. A too-large
        request raises DiskLeaseTooLargeError, which the caller's
        error path turns into a FAILED row with the reason recorded.
        """
        if not self._lease_manager.enabled:
            return None
        total_mb = request_download_size_mb(
            messages,
            unknown_size_mb=settings.link_request_big_file_mb,
        )
        if total_mb <= 0:
            return None
        return await self._lease_manager.acquire(
            total_mb, request_id=request.id
        )

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
