"""
tests/unit/services/test_restricted.py
Unit tests for app.services.restricted — restricted-content fetching.

All Telethon calls are mocked. The processor test uses the real SQLite
test DB (conftest fixtures) to prove the bot->DB->worker queue contract.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.restricted import (
    LinkRequestProcessor,
    NotMemberError,
    RestrictedContentError,
    RestrictedContentService,
    join_by_invite,
)
from app.telegram.link_parser import PostRef

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# fetch_messages
# ---------------------------------------------------------------------------

async def test_fetch_messages_returns_one_per_id():
    client = MagicMock()
    client.get_messages = AsyncMock(side_effect=lambda peer, ids: MagicMock(id=ids))
    svc = RestrictedContentService(client, account_id=1)
    ref = PostRef(peer=-100123, message_ids=(10, 11), is_private=True)
    messages = await svc.fetch_messages(ref)
    assert [m.id for m in messages] == [10, 11]
    assert client.get_messages.await_count == 2


async def test_fetch_from_non_member_chat_raises_not_member():
    from telethon.errors import ChannelPrivateError

    client = MagicMock()
    client.get_messages = AsyncMock(side_effect=ChannelPrivateError(request=None))
    svc = RestrictedContentService(client, account_id=1)
    with pytest.raises(NotMemberError):
        await svc.fetch_messages(PostRef(peer=-100123, message_ids=(1,), is_private=True))


# ---------------------------------------------------------------------------
# join_by_invite
# ---------------------------------------------------------------------------

class _CallableClient:
    """Minimal fake for Telethon's client(request) call style."""

    def __init__(self, side_effect=None, result=None):
        self._side_effect = side_effect
        self._result = result

    async def __call__(self, request):
        if self._side_effect is not None:
            raise self._side_effect
        return self._result


async def test_join_by_invite_already_participant():
    from telethon.errors import UserAlreadyParticipantError

    client = _CallableClient(side_effect=UserAlreadyParticipantError(request=None))
    result = await join_by_invite(client, "somehash")
    assert result == "already a member"


async def test_join_by_invite_expired():
    from telethon.errors import InviteHashExpiredError

    client = _CallableClient(side_effect=InviteHashExpiredError(request=None))
    with pytest.raises(RestrictedContentError, match="expired"):
        await join_by_invite(client, "deadhash")


# ---------------------------------------------------------------------------
# LinkRequestProcessor — queue contract (real DB, mocked Telegram)
# ---------------------------------------------------------------------------

class _SessionCtx:
    """Async CM returning the shared test session (processor opens several
    sessions sequentially; in tests they all map to the one test session)."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


async def test_processor_saves_and_marks_done(db_session, user, account):
    from app.db.models.link_request import LinkRequestStatus
    from app.db.repositories.link_request_repo import LinkRequestRepository

    repo = LinkRequestRepository(db_session)
    request = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/somechannel/42",
    )
    await db_session.commit()
    request_id = request.id

    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    manager = MagicMock()
    manager.get_client = MagicMock(return_value=MagicMock())
    process_message = AsyncMock(return_value="saved")

    processor = LinkRequestProcessor(factory, manager, process_message=process_message)
    # fetch_messages would hit Telegram; stub it out at the service seam.
    fake_message = MagicMock(id=42)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            RestrictedContentService,
            "fetch_messages",
            AsyncMock(return_value=[fake_message]),
        )
        processor._notify_owner = AsyncMock()
        count = await processor.process_pending_once()

    assert count == 1
    process_message.assert_awaited_once()
    fresh = await LinkRequestRepository(db_session).get_by_id(request_id)
    assert fresh.status == LinkRequestStatus.DONE
    assert fresh.result_count == 1


async def test_notify_owner_edits_the_status_message(db_session, user, account):
    """With notify ids on the row, status updates EDIT the bot's one
    status message instead of sending new messages."""
    from unittest.mock import patch

    from app.db.repositories.link_request_repo import LinkRequestRepository
    from app.services.restricted import LinkRequestProcessor

    repo = LinkRequestRepository(db_session)
    request = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/chan/1",
        notify_chat_id=555,
        notify_message_id=777,
    )
    await db_session.commit()
    fresh = await repo.get_by_id(request.id)
    assert fresh.notify_chat_id == 555
    assert fresh.notify_message_id == 777

    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(factory, MagicMock(), process_message=AsyncMock())

    fake_notifier = MagicMock()
    fake_notifier.edit_text = AsyncMock()
    fake_notifier.send_text = AsyncMock()
    with patch(
        "app.services.telegram_notifier.TelegramNotifier",
        return_value=fake_notifier,
    ):
        await processor._notify_owner(fresh, "✅ Done")

    fake_notifier.edit_text.assert_awaited_once_with(
        chat_id=555, message_id=777, text="✅ Done"
    )
    fake_notifier.send_text.assert_not_awaited()


async def test_processor_poll_interval_defaults_to_settings(db_session):
    from app.services.restricted import LinkRequestProcessor

    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(factory, MagicMock(), process_message=AsyncMock())
    assert processor._poll_interval == 3.0


async def test_claim_pending_is_atomic_and_disjoint(db_session, user, account):
    """Claiming flips rows to PROCESSING in one statement: a second
    claim (e.g. another worker process) can never receive the same rows."""
    from app.db.models.link_request import LinkRequestStatus
    from app.db.repositories.link_request_repo import LinkRequestRepository

    repo = LinkRequestRepository(db_session)
    ids = []
    for i in range(3):
        row = await repo.create(
            user_id=user.id,
            telegram_account_id=account.id,
            raw_link=f"https://t.me/chan/{i + 1}",
        )
        ids.append(row.id)
    await db_session.commit()

    first = await repo.claim_pending(limit=2)
    assert [row.id for row in first] == ids[:2]  # oldest first
    assert all(row.status == LinkRequestStatus.PROCESSING for row in first)
    assert all(row.processed_at is not None for row in first)  # claim stamp
    await db_session.commit()

    second = await repo.claim_pending(limit=2)
    assert [row.id for row in second] == ids[2:]
    await db_session.commit()

    assert await repo.claim_pending(limit=2) == []


def _make_run_tracker():
    """A stand-in for _process_request that records temporal overlap.

    The concurrency machinery under test is _run_one (global semaphore +
    per-account lock); the real _process_request's DB path is covered by
    the other processor tests. True concurrent DB sessions cannot run on
    the single-connection in-memory SQLite test DB — the atomic claim
    itself is verified against real PostgreSQL separately.
    """
    import asyncio as _asyncio
    from types import SimpleNamespace

    state = {"active": 0, "max_active": 0}

    async def run_one(request):
        state["active"] += 1
        state["max_active"] = max(state["max_active"], state["active"])
        await _asyncio.sleep(0.05)
        state["active"] -= 1

    def fake_request(request_id, account_id):
        return SimpleNamespace(id=request_id, telegram_account_id=account_id)

    return run_one, state, fake_request


async def test_processor_runs_different_accounts_concurrently(db_session, user, account):
    """The scaling fix: one account's save no longer blocks another's."""
    import asyncio as _asyncio

    run_one, state, fake_request = _make_run_tracker()
    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(factory, MagicMock(), process_message=AsyncMock())
    processor._process_request = run_one

    await _asyncio.gather(
        processor._run_one(fake_request(1, account.id)),
        processor._run_one(fake_request(2, account.id + 1000)),
    )
    assert state["max_active"] == 2  # actually overlapped in time


async def test_processor_serializes_the_same_account(db_session, user, account):
    """Per-account cap of 1: one client's saves stay sequential."""
    import asyncio as _asyncio

    run_one, state, fake_request = _make_run_tracker()
    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(factory, MagicMock(), process_message=AsyncMock())
    processor._process_request = run_one

    await _asyncio.gather(
        processor._run_one(fake_request(1, account.id)),
        processor._run_one(fake_request(2, account.id)),
    )
    assert state["max_active"] == 1


def _budget_processor(db_session, *, cap, budget_mb, rss):
    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(
        factory, MagicMock(), process_message=AsyncMock(),
        max_concurrency=cap, memory_budget_mb=budget_mb,
    )
    processor._rss_reader = lambda: rss
    return processor


async def test_adaptive_limit_shrinks_when_over_budget(db_session):
    """AIMD: over the RAM budget, the claim batch halves down to 1."""
    processor = _budget_processor(db_session, cap=50, budget_mb=100, rss=5000.0)
    assert processor._effective_claim_limit() == 5   # 10 -> 5
    assert processor._effective_claim_limit() == 2   # 5 -> 2
    assert processor._effective_claim_limit() == 1   # 2 -> 1
    assert processor._effective_claim_limit() == 1   # floor


async def test_adaptive_limit_grows_when_under_budget(db_session):
    """Under 80% of budget the batch grows by a quarter, capped."""
    processor = _budget_processor(db_session, cap=50, budget_mb=100, rss=10.0)
    first = processor._effective_claim_limit()
    second = processor._effective_claim_limit()
    assert second > first >= 10
    for _ in range(30):
        limit = processor._effective_claim_limit()
    assert limit == 50  # never beyond the hard cap


async def test_fixed_mode_ignores_rss(db_session):
    """Budget disabled (default): the fixed cap rules, RSS is irrelevant."""
    processor = _budget_processor(db_session, cap=7, budget_mb=0, rss=99999.0)
    assert processor._effective_claim_limit() == 7


async def test_adaptive_claim_processes_only_the_shrunken_batch(db_session, user, account):
    """End-to-end in miniature: over budget, one pass claims 5 of 8."""
    from app.db.models.link_request import LinkRequestStatus
    from app.db.repositories.link_request_repo import LinkRequestRepository
    from app.services.restricted import RestrictedContentService

    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    manager = MagicMock()
    manager.get_client = MagicMock(return_value=MagicMock())
    processor = LinkRequestProcessor(
        factory, manager,
        process_message=AsyncMock(return_value="saved"),
        max_concurrency=50, memory_budget_mb=100,
    )
    processor._rss_reader = lambda: 5000.0  # way over budget
    processor._notify_owner = AsyncMock()
    repo = LinkRequestRepository(db_session)
    for i in range(8):
        await repo.create(user_id=user.id, telegram_account_id=account.id,
                          raw_link=f"https://t.me/chan/{i + 1}")
    await db_session.commit()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(RestrictedContentService, "fetch_messages",
                   AsyncMock(return_value=[MagicMock(id=42)]))
        processed = await processor.process_pending_once()

    assert processed == 5
    remaining = await repo.list_pending(limit=50)
    assert len(remaining) == 3
    assert all(r.status == LinkRequestStatus.PENDING for r in remaining)


def _budget_only_processor(db_session, *, rss, free_mb):
    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(
        factory, MagicMock(), process_message=AsyncMock(),
        max_concurrency=0, memory_budget_mb=3072,
    )
    processor._rss_reader = lambda: rss
    processor._temp_free_reader = lambda: free_mb
    return processor


async def test_budget_only_removes_the_slot_cap(db_session):
    """Cap 0 + budget: no human ceiling; RAM sizes the batch."""
    processor = _budget_only_processor(db_session, rss=100.0, free_mb=900_000.0)
    assert processor._budget_only is True
    assert processor._max_concurrency == 100_000
    # RAM comfortably under budget -> batch grows past any old cap.
    assert processor._effective_claim_limit() == 12  # 10 + 25%


async def test_budget_only_disk_guard_binds(db_session):
    """RAM cannot see the disk: 900 MB free temp = 3 slots, no more."""
    processor = _budget_only_processor(db_session, rss=100.0, free_mb=900.0)
    assert processor._effective_claim_limit() == 3  # 900 // 300 reserve


async def test_zero_cap_without_budget_falls_back_to_default(db_session):
    """Cap 0 with NO budget must not leave the queue unguarded."""
    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    processor = LinkRequestProcessor(
        factory, MagicMock(), process_message=AsyncMock(),
        max_concurrency=0, memory_budget_mb=0,
    )
    assert processor._budget_only is False
    assert processor._effective_claim_limit() == 10


async def test_stale_processing_requests_are_reset(db_session, user, account):
    """A request left in PROCESSING by a stopped worker must return to
    PENDING at startup, or it would never be retried."""
    from app.db.models.link_request import LinkRequestStatus
    from app.db.repositories.link_request_repo import LinkRequestRepository

    repo = LinkRequestRepository(db_session)
    request = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/chan/1",
    )
    await repo.mark(request, status=LinkRequestStatus.PROCESSING)
    await db_session.commit()

    reset = await repo.reset_processing_to_pending()
    assert reset == 1
    fresh = await repo.get_by_id(request.id)
    assert fresh.status == LinkRequestStatus.PENDING


async def test_processor_marks_failed_without_client(db_session, user, account):
    from app.db.models.link_request import LinkRequestStatus
    from app.db.repositories.link_request_repo import LinkRequestRepository

    repo = LinkRequestRepository(db_session)
    request = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link="https://t.me/c/1234567/890",
    )
    await db_session.commit()
    request_id = request.id

    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    manager = MagicMock()
    manager.get_client = MagicMock(return_value=None)  # worker has no client running
    processor = LinkRequestProcessor(factory, manager, process_message=AsyncMock())
    processor._notify_owner = AsyncMock()

    await processor.process_pending_once()

    fresh = await LinkRequestRepository(db_session).get_by_id(request_id)
    assert fresh.status == LinkRequestStatus.FAILED
    assert "not running" in (fresh.error or "")
