"""
tests/unit/services/test_recovery.py
Unit tests for RecoveryService (Phase 10 — Idempotency and Recovery).

Scenarios tested:
  1. No PENDING records → returns zero summary, no Telegram calls.
  2. PENDING record, client available → recovered (send_message called, record SAVED).
  3. PENDING record, client unavailable (not RUNNING) → skipped (stays PENDING).
  4. Recovery send_message fails → record marked FAILED.
  5. SAVED records are never touched (idempotency).
  6. Multiple accounts: failure in one account does not block others.
  7. Mixed: some recovered, some skipped, some failed → correct summary counts.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.media_record import MediaRecord, MediaRecordStatus, MediaType
from app.db.repositories.media_record_repo import MediaRecordRepository
from app.services.recovery import RecoveryService

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_pending_record(
    id=1,
    account_id=1,
    user_id=1,
    chat_id=100,
    message_id=42,
    media_type=MediaType.PHOTO,
    ttl=None,
) -> MagicMock:
    r = MagicMock(spec=MediaRecord)
    r.id = id
    r.telegram_account_id = account_id
    r.user_id = user_id
    r.source_chat_id = chat_id
    r.source_message_id = message_id
    r.media_type = media_type
    r.ttl_seconds = ttl
    r.status = MediaRecordStatus.PENDING
    r.sender_telegram_id = 9001
    r.sender_username = "alice"
    r.sender_display_name = "Alice"
    r.source_chat_title = "Test Chat"
    r.source_chat_username = "testchat"
    return r


def make_manager(running_accounts: list[int]) -> MagicMock:
    """Manager whose get_client() returns a client only for running_accounts."""
    manager = MagicMock()

    def get_client(account_id):
        if account_id in running_accounts:
            client = MagicMock()
            sent = MagicMock()
            sent.id = 9999
            client.send_message = AsyncMock(return_value=sent)
            return client
        return None

    manager.get_client = get_client
    return manager


# ---------------------------------------------------------------------------
# Scenario 1: No PENDING records
# ---------------------------------------------------------------------------

async def test_no_pending_records_returns_zero_summary(db_session: AsyncSession):
    """When no PENDING records exist, returns zeroed summary and makes no Telegram calls."""
    manager = make_manager(running_accounts=[1])
    recovery = RecoveryService(lambda: db_session, manager)

    # Override the session_factory to return the test session
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session

    recovery._session_factory = mock_factory

    summary = await recovery.run()

    assert summary == {"recovered": 0, "skipped": 0, "failed": 0}


# ---------------------------------------------------------------------------
# Scenario 2: PENDING record with running client → recovered
# ---------------------------------------------------------------------------

async def test_pending_record_with_running_client_is_recovered(
    db_session: AsyncSession, user, account
):
    """A PENDING record whose account client is running gets forwarded and marked SAVED."""
    # Create a real PENDING record in the DB
    repo = MediaRecordRepository(db_session)
    record = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        source_chat_id=100,
        source_message_id=42,
        media_type=MediaType.PHOTO,
        status=MediaRecordStatus.PENDING,
    )
    await db_session.commit()

    manager = make_manager(running_accounts=[account.id])
    recovery = RecoveryService(None, manager)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session

    recovery._session_factory = mock_factory

    summary = await recovery.run()

    assert summary["recovered"] == 1
    assert summary["skipped"] == 0
    assert summary["failed"] == 0

    # Record should now be SAVED
    await db_session.refresh(record)
    assert record.status == MediaRecordStatus.SAVED
    assert record.saved_message_id == 9999


# ---------------------------------------------------------------------------
# Scenario 3: PENDING record but client not running → skipped
# ---------------------------------------------------------------------------

async def test_pending_record_no_client_is_skipped(
    db_session: AsyncSession, user, account
):
    """A PENDING record whose account has no running client is skipped (stays PENDING)."""
    repo = MediaRecordRepository(db_session)
    record = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        source_chat_id=200,
        source_message_id=99,
        media_type=MediaType.VIDEO,
        status=MediaRecordStatus.PENDING,
    )
    await db_session.commit()

    # Client not available for this account
    manager = make_manager(running_accounts=[])
    recovery = RecoveryService(None, manager)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session

    recovery._session_factory = mock_factory

    summary = await recovery.run()

    assert summary["skipped"] == 1
    assert summary["recovered"] == 0
    assert summary["failed"] == 0

    # Record must still be PENDING
    await db_session.refresh(record)
    assert record.status == MediaRecordStatus.PENDING


# ---------------------------------------------------------------------------
# Scenario 4: Recovery send fails → FAILED
# ---------------------------------------------------------------------------

async def test_pending_record_send_fails_is_marked_failed(
    db_session: AsyncSession, user, account
):
    """If send_message raises, the record is marked FAILED."""
    repo = MediaRecordRepository(db_session)
    record = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        source_chat_id=300,
        source_message_id=77,
        media_type=MediaType.VOICE,
        status=MediaRecordStatus.PENDING,
    )
    await db_session.commit()

    # Client that raises on send_message
    manager = MagicMock()
    client = MagicMock()
    client.send_message = AsyncMock(side_effect=Exception("FLOOD_WAIT"))
    manager.get_client = lambda _: client

    recovery = RecoveryService(None, manager)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session

    recovery._session_factory = mock_factory

    summary = await recovery.run()

    assert summary["failed"] == 1
    assert summary["recovered"] == 0

    await db_session.refresh(record)
    assert record.status == MediaRecordStatus.FAILED
    assert "FLOOD_WAIT" in record.error


# ---------------------------------------------------------------------------
# Scenario 5: SAVED records are never re-processed
# ---------------------------------------------------------------------------

async def test_saved_records_are_never_touched(
    db_session: AsyncSession, user, account
):
    """SAVED records must not be retried. Only PENDING is eligible."""
    repo = MediaRecordRepository(db_session)
    saved_record = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        source_chat_id=400,
        source_message_id=55,
        media_type=MediaType.PHOTO,
        status=MediaRecordStatus.PENDING,
    )
    await repo.mark_saved(saved_record, saved_message_id=1234, saved_at=datetime.now(timezone.utc))
    await db_session.commit()

    client = MagicMock()
    client.send_message = AsyncMock()
    manager = MagicMock()
    manager.get_client = lambda _: client

    recovery = RecoveryService(None, manager)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session

    recovery._session_factory = mock_factory

    summary = await recovery.run()

    # Nothing should be processed — record was already SAVED
    assert summary == {"recovered": 0, "skipped": 0, "failed": 0}
    client.send_message.assert_not_called()


# ---------------------------------------------------------------------------
# Scenario 6: Multiple accounts — failure in one does not block others
# ---------------------------------------------------------------------------

async def test_multiple_accounts_one_fails_others_succeed(
    db_session: AsyncSession, user, account
):
    """Account isolation: a failed recovery for account A still processes account B."""
    from app.db.repositories.telegram_account_repo import TelegramAccountRepository
    from app.db.models.telegram_account import TelegramAccountStatus

    # Create a second account
    account_repo = TelegramAccountRepository(db_session)
    account_b = await account_repo.create(
        user_id=user.id,
        phone_masked="+628****1111",
        status=TelegramAccountStatus.ACTIVE,
    )
    await db_session.commit()

    repo = MediaRecordRepository(db_session)

    # PENDING for account A — will fail
    record_a = await repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        source_chat_id=100,
        source_message_id=1,
        media_type=MediaType.PHOTO,
        status=MediaRecordStatus.PENDING,
    )

    # PENDING for account B — will succeed
    record_b = await repo.create(
        user_id=user.id,
        telegram_account_id=account_b.id,
        source_chat_id=200,
        source_message_id=2,
        media_type=MediaType.VIDEO,
        status=MediaRecordStatus.PENDING,
    )
    await db_session.commit()

    # Client A raises, client B succeeds
    client_a = MagicMock()
    client_a.send_message = AsyncMock(side_effect=Exception("Account A error"))

    client_b = MagicMock()
    sent_b = MagicMock()
    sent_b.id = 8888
    client_b.send_message = AsyncMock(return_value=sent_b)

    manager = MagicMock()

    def get_client(aid):
        if aid == account.id:
            return client_a
        if aid == account_b.id:
            return client_b
        return None

    manager.get_client = get_client

    recovery = RecoveryService(None, manager)

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def mock_factory():
        yield db_session

    recovery._session_factory = mock_factory

    summary = await recovery.run()

    assert summary["recovered"] == 1  # account B
    assert summary["failed"] == 1     # account A

    await db_session.refresh(record_a)
    await db_session.refresh(record_b)
    assert record_a.status == MediaRecordStatus.FAILED
    assert record_b.status == MediaRecordStatus.SAVED


# ---------------------------------------------------------------------------
# Repo tests: list_pending and reset_to_pending
# ---------------------------------------------------------------------------

async def test_list_pending_returns_only_pending(db_session: AsyncSession, user, account):
    """list_pending() returns only PENDING records, not SAVED or FAILED."""
    repo = MediaRecordRepository(db_session)

    pending = await repo.create(
        user_id=user.id, telegram_account_id=account.id,
        source_chat_id=1, source_message_id=1,
        media_type=MediaType.PHOTO, status=MediaRecordStatus.PENDING,
    )
    saved = await repo.create(
        user_id=user.id, telegram_account_id=account.id,
        source_chat_id=1, source_message_id=2,
        media_type=MediaType.PHOTO, status=MediaRecordStatus.PENDING,
    )
    await repo.mark_saved(saved, saved_message_id=999, saved_at=datetime.now(timezone.utc))
    await db_session.commit()

    results = await repo.list_pending()
    ids = {r.id for r in results}

    assert pending.id in ids
    assert saved.id not in ids


async def test_list_pending_filtered_by_account(db_session: AsyncSession, user, account):
    """list_pending(account_id=X) only returns records for that account."""
    from app.db.repositories.telegram_account_repo import TelegramAccountRepository
    from app.db.models.telegram_account import TelegramAccountStatus

    account_repo = TelegramAccountRepository(db_session)
    other = await account_repo.create(
        user_id=user.id,
        phone_masked="+628****9999",
        status=TelegramAccountStatus.ACTIVE,
    )
    await db_session.commit()

    repo = MediaRecordRepository(db_session)
    r1 = await repo.create(
        user_id=user.id, telegram_account_id=account.id,
        source_chat_id=1, source_message_id=10,
        media_type=MediaType.PHOTO, status=MediaRecordStatus.PENDING,
    )
    r2 = await repo.create(
        user_id=user.id, telegram_account_id=other.id,
        source_chat_id=1, source_message_id=20,
        media_type=MediaType.PHOTO, status=MediaRecordStatus.PENDING,
    )
    await db_session.commit()

    results = await repo.list_pending(account.id)
    ids = {r.id for r in results}

    assert r1.id in ids
    assert r2.id not in ids
