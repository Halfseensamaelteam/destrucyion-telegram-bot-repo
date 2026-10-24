"""
tests/unit/services/test_story.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Story fetching + the worker story path, all with a fake Telethon client.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.db.models.link_request import LinkRequestStatus
from app.db.repositories.link_request_repo import LinkRequestRepository
from app.services.restricted import LinkRequestProcessor, RestrictedContentError
from app.services.story import StoryService, story_media_size_bytes
from app.telegram.link_parser import StoryRef

pytestmark = pytest.mark.asyncio


def _story(story_id, *, size=None, photo_sizes=None, caption="hello", media=True):
    if not media:
        return SimpleNamespace(id=story_id, media=None, caption=caption)
    if photo_sizes is not None:
        media_obj = SimpleNamespace(
            document=None,
            photo=SimpleNamespace(
                sizes=[SimpleNamespace(size=s) for s in photo_sizes]
            ),
        )
    else:
        media_obj = SimpleNamespace(
            document=SimpleNamespace(size=size), photo=None
        )
    return SimpleNamespace(id=story_id, media=media_obj, caption=caption)


class FakeStoryClient:
    """Minimal fake for the Telethon stories calls the worker makes."""

    def __init__(self, *, by_id=None, active=None, pinned=None):
        self._by_id = by_id or []
        self._active = active or []
        self._pinned = pinned or []
        self.input_peers = []
        self.download_media = AsyncMock(return_value="/tmp/story_file")
        self.send_file = AsyncMock()

    async def get_input_entity(self, username):
        self.input_peers.append(username)
        return f"INPUT:{username}"

    async def __call__(self, request):
        name = type(request).__name__
        if name == "GetStoriesByIDRequest":
            return SimpleNamespace(stories=list(self._by_id))
        if name == "GetPeerStoriesRequest":
            return SimpleNamespace(stories=list(self._active))
        if name == "GetPinnedStoriesRequest":
            return SimpleNamespace(stories=list(self._pinned))
        raise AssertionError(f"unexpected request {name}")


class _SessionCtx:
    """Async CM returning the shared test session."""

    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class _RecordingLeaseManager:
    """Stand-in for DiskLeaseManager: records acquire/release calls.

    The real manager is host-disk dependent (it fail-fasts when free
    space is below the reserve — this VM's /tmp is a 512 MB tmpfs),
    so story tests inject this stub and assert the story path leases
    the right byte total instead of depending on host disk.
    """

    def __init__(self):
        self.acquired = []
        self.released = []

    @property
    def enabled(self):
        return True

    async def acquire(self, total_mb, request_id=None):
        self.acquired.append((total_mb, request_id))
        lease = MagicMock()
        lease.release = lambda: self.released.append(total_mb)
        return lease


async def _run_story_row(db_session, user, account, raw_link, client):
    repo = LinkRequestRepository(db_session)
    request = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link=raw_link,
    )
    await db_session.commit()
    request_id = request.id

    factory = lambda: _SessionCtx(db_session)  # noqa: E731
    manager = MagicMock()
    manager.get_client = MagicMock(return_value=client)
    leases = _RecordingLeaseManager()
    processor = LinkRequestProcessor(
        factory, manager, process_message=AsyncMock(), lease_manager=leases
    )
    processor._notify_owner = AsyncMock()
    count = await processor.process_pending_once()

    fresh = await LinkRequestRepository(db_session).get_by_id(request_id)
    return count, fresh, processor, leases


# ---------------------------------------------------------------------------
# story_media_size_bytes
# ---------------------------------------------------------------------------

async def test_media_size_document():
    assert story_media_size_bytes(_story(1, size=12345)) == 12345


async def test_media_size_photo_uses_largest_variant():
    assert story_media_size_bytes(_story(1, photo_sizes=[100, 900, 400])) == 900


async def test_media_size_none_without_media():
    assert story_media_size_bytes(_story(1, media=False)) is None


# ---------------------------------------------------------------------------
# StoryService.fetch_single / fetch_all
# ---------------------------------------------------------------------------

async def test_fetch_single_returns_story():
    client = FakeStoryClient(by_id=[_story(7)])
    svc = StoryService(client, account_id=1)
    story = await svc.fetch_single(StoryRef(peer="someone", story_id=7))
    assert story.id == 7
    assert client.input_peers == ["someone"]


async def test_fetch_single_expired_raises_clear_error():
    client = FakeStoryClient(by_id=[])
    svc = StoryService(client, account_id=1)
    with pytest.raises(RestrictedContentError, match="no longer available"):
        await svc.fetch_single(StoryRef(peer="someone", story_id=7))


async def test_fetch_all_dedups_active_and_pinned():
    client = FakeStoryClient(
        active=[_story(1), _story(2)],
        pinned=[_story(2), _story(3)],
    )
    svc = StoryService(client, account_id=1)
    stories = await svc.fetch_all(StoryRef(peer="someone"))
    assert [s.id for s in stories] == [1, 2, 3]


async def test_fetch_all_empty_is_not_an_error():
    client = FakeStoryClient(active=[], pinned=[])
    svc = StoryService(client, account_id=1)
    assert await svc.fetch_all(StoryRef(peer="someone")) == []


# ---------------------------------------------------------------------------
# Processor story path (real DB, fake Telethon)
# ---------------------------------------------------------------------------

async def test_processor_saves_single_story(db_session, user, account):
    client = FakeStoryClient(by_id=[_story(7, size=2048, caption="hi there")])
    count, fresh, _, _leases = await _run_story_row(
        db_session, user, account, "https://t.me/someone/s/7", client
    )
    assert count == 1
    assert fresh.status == LinkRequestStatus.DONE
    assert fresh.result_count == 1
    client.download_media.assert_awaited_once()
    _, kwargs = client.send_file.await_args
    assert kwargs["caption"] == "hi there"


async def test_processor_saves_all_active_stories(db_session, user, account):
    client = FakeStoryClient(
        active=[_story(1, size=100), _story(2, size=200)],
        pinned=[],
    )
    count, fresh, _, _leases = await _run_story_row(
        db_session, user, account, "https://t.me/someone", client
    )
    assert count == 1
    assert fresh.status == LinkRequestStatus.DONE
    assert fresh.result_count == 2
    assert client.send_file.await_count == 2


async def test_processor_expired_story_fails_cleanly(db_session, user, account):
    client = FakeStoryClient(by_id=[])
    count, fresh, processor, _leases = await _run_story_row(
        db_session, user, account, "https://t.me/someone/s/7", client
    )
    assert count == 1
    assert fresh.status == LinkRequestStatus.FAILED
    assert "no longer available" in fresh.error
    client.send_file.assert_not_awaited()
    notified = " ".join(
        c.args[1] for c in processor._notify_owner.await_args_list
    )
    assert "no longer available" in notified


async def test_processor_skips_media_less_stories(db_session, user, account):
    client = FakeStoryClient(
        active=[_story(1, media=False), _story(2, size=300)],
        pinned=[],
    )
    count, fresh, _, _leases = await _run_story_row(
        db_session, user, account, "https://t.me/someone", client
    )
    assert fresh.status == LinkRequestStatus.DONE
    assert fresh.result_count == 1
    assert client.send_file.await_count == 1


async def test_processor_all_media_less_fails_cleanly(db_session, user, account):
    client = FakeStoryClient(active=[_story(1, media=False)], pinned=[])
    count, fresh, _, _leases = await _run_story_row(
        db_session, user, account, "https://t.me/someone", client
    )
    assert fresh.status == LinkRequestStatus.FAILED
    assert "no photo or video" in fresh.error


async def test_story_path_leases_its_media_bytes(db_session, user, account):
    """The story download leases exactly its media bytes, then releases."""
    client = FakeStoryClient(
        active=[_story(1, size=1048576), _story(2, size=2097152)],
        pinned=[],
    )
    count, fresh, _, leases = await _run_story_row(
        db_session, user, account, "https://t.me/someone", client
    )
    assert fresh.status == LinkRequestStatus.DONE
    assert len(leases.acquired) == 1
    assert leases.acquired[0][0] == pytest.approx(3.0)
    assert leases.released == [pytest.approx(3.0)]
