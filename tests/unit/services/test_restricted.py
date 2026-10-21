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
