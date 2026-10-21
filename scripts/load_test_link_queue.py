#!/usr/bin/env python3
"""Load-test the /save queue pipeline with FAKE Telethon clients.

What this exercises (all real production code):
  LinkRequest rows in a real database -> LinkRequestProcessor atomic
  claiming -> concurrent processing (global cap + per-account lock) ->
  events._process_message -> SavedMessagesService download+upload path
  (including the parallel multi-connection downloader).

What is fake: only the Telegram network side. Each account gets a
FakeTelegramClient that serves one fixed "video" of --file-mb megabytes
at a simulated per-connection rate, and accepts uploads at a simulated
rate. No real Telegram account, session, or network is involved.

What this proves: queue/claim/concurrency behaviour and throughput of
OUR code at --accounts scale. What it cannot prove: real Telegram
limits (FloodWait, per-IP throttling, real MTProto speeds) — those need
real accounts (2 are enough for a smoke test).

SAFETY: the processor claims EVERY pending link_requests row in the
target database. NEVER point this at a production database — claimed
real requests would be "saved" by fake clients and marked DONE.
Use a scratch database and pass --confirm-scratch-db to prove you read
this. Default is a throwaway SQLite file (loadtest.db in the cwd).

Examples:
  # quick smoke: 10 accounts, one 20 MB video each
  python scripts/load_test_link_queue.py --confirm-scratch-db \
      --accounts 10 --file-mb 20

  # the 100-account run, against a scratch PostgreSQL on the VPS
  python scripts/load_test_link_queue.py --confirm-scratch-db \
      --accounts 100 --file-mb 50 \
      --database-url postgresql+asyncpg://user:pass@127.0.0.1/loadtest

Rates are PER CONNECTION (each of the DOWNLOAD_WORKERS streams of a
file, and each upload). Aggregate simulated bandwidth is therefore
streams x rate; absolute wall-times are simulated, the concurrency
behaviour and queue correctness are real.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

# Settings must load with harmless dummy values before app imports.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("APP_SECRET_KEY", "load-test-secret")
os.environ.setdefault("TELEGRAM_API_ID", "12345")
os.environ.setdefault("TELEGRAM_API_HASH", "loadtesthash")
os.environ.setdefault("BOT_TOKEN", "123456:load-test")
os.environ.setdefault("RESTRICTED_BATCH_DELAY_SECONDS", "0")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CHUNK = 512 * 1024  # matches the parallel downloader's part size


@dataclass
class Stats:
    active_streams: int = 0
    max_streams: int = 0
    active_uploads: int = 0
    max_uploads: int = 0
    bytes_down: int = 0
    bytes_up: int = 0
    uploads: int = 0
    sent_ids: int = 0


class FakeFile:
    """Telethon's Message.file stand-in (size/name/ext only)."""

    def __init__(self, size: int, name: str = "loadtest.mp4") -> None:
        self.size = size
        self.name = name
        self.ext = ".mp4"
        self.mime_type = "video/mp4"


class FakeMessage:
    """A protected (noforwards) video post, built on real Telethon TL
    types so classify_message() and the resend path treat it exactly
    like a genuine channel post."""

    def __init__(self, payload_size: int) -> None:
        from telethon.tl.types import (
            Document,
            DocumentAttributeFilename,
            DocumentAttributeVideo,
            MessageMediaDocument,
        )

        self.document = Document(
            id=777001,
            access_hash=0,
            file_reference=b"",
            date=datetime.now(timezone.utc),
            mime_type="video/mp4",
            size=payload_size,
            thumbs=[],
            dc_id=2,
            attributes=[
                DocumentAttributeVideo(
                    duration=60, w=1280, h=720, supports_streaming=True
                ),
                DocumentAttributeFilename(file_name="loadtest.mp4"),
            ],
        )
        self.media = MessageMediaDocument(document=self.document, ttl_seconds=None)
        self.id = 1
        self.chat_id = -1001234567890
        self.message = "load test caption"
        self.noforwards = True  # restricted content -> download+upload path
        self._file = FakeFile(payload_size)

    @property
    def file(self) -> FakeFile:
        return self._file

    async def get_chat(self):
        return None

    async def get_sender(self):
        return None


class FakeTelegramClient:
    """Serves one fixed video at a simulated per-connection rate."""

    def __init__(self, stats: Stats, message: FakeMessage, payload: bytes,
                 download_bps: float, upload_bps: float) -> None:
        self._stats = stats
        self._message = message
        self._payload = payload
        self._download_bps = download_bps
        self._upload_bps = upload_bps

    async def get_messages(self, peer, ids=None):
        return self._message

    async def _pace(self, nbytes: int, bps: float) -> None:
        if bps > 0:
            await asyncio.sleep(nbytes / bps)

    def iter_download(self, source, *, offset=0, stride=None,
                      chunk_size=None, request_size=None):
        stats = self._stats
        payload = self._payload
        total = len(payload)
        step = request_size or CHUNK
        bps = self._download_bps

        async def _gen():
            stats.active_streams += 1
            stats.max_streams = max(stats.max_streams, stats.active_streams)
            try:
                pos = offset
                while pos < total:
                    end = min(pos + step, total)
                    chunk = payload[pos:end]
                    await self._pace(len(chunk), bps)
                    stats.bytes_down += len(chunk)
                    yield chunk
                    pos = pos + stride if stride else end
            finally:
                stats.active_streams -= 1

        return _gen()

    async def download_media(self, source, *, file=None, thumb=None,
                             progress_callback=None, **kwargs):
        if thumb is not None:
            return None  # no thumbnail in the fake world
        stats = self._stats
        stats.active_streams += 1
        stats.max_streams = max(stats.max_streams, stats.active_streams)
        try:
            dest = file
            if dest is None:
                return None
            if os.path.isdir(dest):
                dest = os.path.join(dest, "loadtest.mp4")
            pos = 0
            total = len(self._payload)
            with open(dest, "wb") as fh:
                while pos < total:
                    end = min(pos + CHUNK, total)
                    chunk = self._payload[pos:end]
                    await self._pace(len(chunk), self._download_bps)
                    fh.write(chunk)
                    stats.bytes_down += len(chunk)
                    if progress_callback is not None:
                        progress_callback(end, total)
                    pos = end
            return dest
        finally:
            stats.active_streams -= 1

    async def send_file(self, **kwargs):
        stats = self._stats
        path = kwargs.get("file")
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        stats.active_uploads += 1
        stats.max_uploads = max(stats.max_uploads, stats.active_uploads)
        try:
            # Pace in chunks so concurrent uploads interleave realistically.
            remaining = size
            while remaining > 0:
                step = min(CHUNK, remaining)
                await self._pace(step, self._upload_bps)
                stats.bytes_up += step
                remaining -= step
        finally:
            stats.active_uploads -= 1
        stats.uploads += 1
        stats.sent_ids += 1
        return SimpleNamespace(id=900000 + stats.sent_ids)


class FakeClientManager:
    """Stand-in for worker.supervisor.TelegramClientManager: the two
    surfaces the pipeline uses are ._clients[account_id].user_id and
    .get_client(account_id)."""

    def __init__(self) -> None:
        self._clients: dict[int, SimpleNamespace] = {}
        self._telegram: dict[int, FakeTelegramClient] = {}

    def add(self, account_id: int, user_id: int, client: FakeTelegramClient) -> None:
        self._clients[account_id] = SimpleNamespace(user_id=user_id)
        self._telegram[account_id] = client

    def get_client(self, account_id: int):
        return self._telegram.get(account_id)


async def _no_notify(self, request, text) -> None:
    """Owner notifications go through the real Bot API; they are a
    courtesy side-channel, not part of the transfer load measured here."""
    return None


async def run(args: argparse.Namespace) -> int:
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

    import app.db.models  # noqa: F401 — register models
    from app.core.config import get_settings
    from app.db.models.base import Base
    from app.db.models.link_request import LinkRequest, LinkRequestStatus
    from app.db.models.telegram_account import TelegramAccountStatus
    from app.db.repositories.link_request_repo import LinkRequestRepository
    from app.db.repositories.telegram_account_repo import TelegramAccountRepository
    from app.db.repositories.user_repo import UserRepository
    from app.services.restricted import LinkRequestProcessor
    from app.telegram.events import _process_message

    settings = get_settings()

    # Temp-space preflight: every in-flight save holds one full temp
    # copy of the file (the downloader preallocates it), so the temp
    # dir needs concurrency x file size free — running out of disk
    # mid-transfer is exactly the failure this test would otherwise
    # report confusingly. (Same math applies to a production VPS.)
    import shutil
    import tempfile

    in_flight = min(settings.link_request_max_concurrency, args.accounts)
    needed_mb = in_flight * args.file_mb * 1.5
    free_mb = shutil.disk_usage(tempfile.gettempdir()).free / 1e6
    print(f"temp dir: {tempfile.gettempdir()}  free={free_mb:.0f} MB  "
          f"needed~{needed_mb:.0f} MB ({in_flight} in flight x {args.file_mb} MB x1.5)")
    if free_mb < needed_mb:
        print("ERROR: not enough temp space for this run — lower --accounts/"
              "--file-mb, raise LINK_REQUEST_MAX_CONCURRENCY down, or point "
              "TMPDIR at a bigger disk.")
        return 2

    print(f"database: {args.database_url}")
    print(f"accounts={args.accounts} requests/account={args.requests_per_account} "
          f"file={args.file_mb} MB download={args.download_mbps} MB/s/conn "
          f"upload={args.upload_mbps} MB/s queue_concurrency="
          f"{settings.link_request_max_concurrency} download_workers="
          f"{settings.download_workers}")

    if args.database_url.startswith("sqlite"):
        from sqlalchemy import event

        engine = create_async_engine(
            args.database_url, connect_args={"timeout": 60}
        )

        @event.listens_for(engine.sync_engine, "connect")
        def _wal(dbapi_conn, _record):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA busy_timeout=60000")
            cur.close()
    else:
        engine = create_async_engine(args.database_url)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession,
                                 expire_on_commit=False)

    # ---- seed fake users/accounts + pending link requests -------------
    file_size = int(args.file_mb * 1024 * 1024)
    payload = (b"destrucyion-loadtest!" * (file_size // 21 + 1))[:file_size]
    message = FakeMessage(file_size)
    stats = Stats()
    manager = FakeClientManager()
    base_tg_id = 900_000_000 + int(time.time()) % 100_000
    request_ids: list[int] = []

    async with factory() as session:
        user_repo = UserRepository(session)
        account_repo = TelegramAccountRepository(session)
        link_repo = LinkRequestRepository(session)
        for i in range(args.accounts):
            user = await user_repo.create(telegram_user_id=base_tg_id + i)
            account = await account_repo.create(
                user_id=user.id,
                phone_masked=f"+628****{i:04d}",
                status=TelegramAccountStatus.ACTIVE,
            )
            manager.add(
                account.id,
                user.id,
                FakeTelegramClient(
                    stats, message, payload,
                    download_bps=args.download_mbps * 1024 * 1024,
                    upload_bps=args.upload_mbps * 1024 * 1024,
                ),
            )
            for _ in range(args.requests_per_account):
                row = await link_repo.create(
                    user_id=user.id,
                    telegram_account_id=account.id,
                    raw_link="https://t.me/loadtestchan/1",
                )
                request_ids.append(row.id)
        await session.commit()
    total_requests = len(request_ids)
    print(f"seeded {total_requests} pending requests for {args.accounts} accounts")

    # ---- drain the queue with the real processor ----------------------
    processor = LinkRequestProcessor(factory, manager, process_message=_process_message)
    processor._notify_owner = _no_notify.__get__(processor, LinkRequestProcessor)

    started = time.monotonic()
    deadline = started + args.timeout
    while time.monotonic() < deadline:
        claimed = await processor.process_pending_once()
        async with factory() as session:
            counts = dict(
                (await session.execute(
                    select(LinkRequest.status, func.count())
                    .where(LinkRequest.id.in_(request_ids))
                    .group_by(LinkRequest.status)
                )).all()
            )
        remaining = sum(
            c for s, c in counts.items()
            if s in (LinkRequestStatus.PENDING, LinkRequestStatus.PROCESSING)
        )
        if claimed == 0 and remaining == 0:
            break
        if claimed == 0:
            await asyncio.sleep(0.25)
    wall = time.monotonic() - started

    # ---- report --------------------------------------------------------
    async with factory() as session:
        rows = list((await session.execute(
            select(LinkRequest).where(LinkRequest.id.in_(request_ids))
        )).scalars().all())
    await engine.dispose()

    done = [r for r in rows if r.status == LinkRequestStatus.DONE]
    failed = [r for r in rows if r.status == LinkRequestStatus.FAILED]
    latencies = sorted(
        (r.processed_at - r.created_at).total_seconds()
        for r in done
        if r.processed_at is not None and r.created_at is not None
    )

    def _pct(p: float) -> float:
        if not latencies:
            return 0.0
        return latencies[min(len(latencies) - 1, int(len(latencies) * p))]

    print("\n================ LOAD TEST RESULT ================")
    print(f"requests:        {total_requests}  done={len(done)}  failed={len(failed)}")
    print(f"wall time:       {wall:.1f}s  ({total_requests / max(wall, 0.001):.2f} req/s)")
    print(f"max download streams active at once: {stats.max_streams}")
    print(f"max uploads active at once:          {stats.max_uploads}")
    print(f"bytes downloaded: {stats.bytes_down / 1e6:.1f} MB   "
          f"uploaded: {stats.bytes_up / 1e6:.1f} MB   uploads: {stats.uploads}")
    if latencies:
        print(f"per-request latency (queue->done): avg={sum(latencies)/len(latencies):.1f}s "
              f"p50={_pct(0.50):.1f}s p95={_pct(0.95):.1f}s max={latencies[-1]:.1f}s")
    if failed:
        print("first failure:", failed[0].error)
    ok = len(done) == total_requests and stats.uploads == total_requests
    print("verdict:", "PASS — every request saved exactly once" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--accounts", type=int, default=10)
    parser.add_argument("--requests-per-account", type=int, default=1)
    parser.add_argument("--file-mb", type=float, default=20)
    parser.add_argument("--download-mbps", type=float, default=1.5,
                        help="simulated per-connection download MB/s")
    parser.add_argument("--upload-mbps", type=float, default=3.0,
                        help="simulated per-connection upload MB/s")
    parser.add_argument("--database-url", default="sqlite+aiosqlite:///./loadtest.db")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--confirm-scratch-db", action="store_true",
                        help="required: confirms the target DB is a scratch DB")
    args = parser.parse_args()
    if not args.confirm_scratch_db:
        parser.error("refusing to run without --confirm-scratch-db "
                     "(see the safety note in this file's docstring)")
    if args.database_url == "sqlite+aiosqlite:///./loadtest.db" and os.path.exists("loadtest.db"):
        os.remove("loadtest.db")  # our own default scratch file
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
