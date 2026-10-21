#!/usr/bin/env python3
"""Load-test the /save queue pipeline with FAKE Telethon clients.

What this exercises (all real production code):
  LinkRequest rows in a real database -> LinkRequestProcessor atomic
  claiming -> concurrent processing (global cap + per-account lock) ->
  events._process_message -> SavedMessagesService download+upload path
  (including the parallel multi-connection downloader).

What is fake: only the Telegram network side. Each account gets a
FakeTelegramClient serving "videos" at simulated per-connection rates
(each account's rate is jittered, like real users on different links).
No real Telegram account, session, or network is involved.

Workload: every account saves a random number of videos, each of a
random size in [--file-min-mb, --file-max-mb] — accounts work through
their own queues independently, so at any moment one user may be
downloading 180 MB while another finishes a 30 MB one.

Display: a live terminal dashboard (account wall + per-transfer
progress bars) when stdout is a TTY; plain periodic summary lines
otherwise (e.g. docker logs). --no-live forces the plain mode.

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
  # quick smoke: 10 accounts, mixed 5-12 MB videos
  python scripts/load_test_link_queue.py --confirm-scratch-db \
      --accounts 10 --file-min-mb 5 --file-max-mb 12 --requests-per-account 2-3

  # the 100-user run, against a scratch PostgreSQL on the VPS
  python scripts/load_test_link_queue.py --confirm-scratch-db \
      --accounts 100 --file-min-mb 30 --file-max-mb 200 \
      --database-url postgresql+asyncpg://user:pass@127.0.0.1/loadtest

Rates are PER CONNECTION (each of the DOWNLOAD_WORKERS streams of a
file, and each upload), jittered per account. Absolute wall-times are
simulated; the concurrency behaviour and queue correctness are real.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
from dataclasses import dataclass
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
MB = 1024 * 1024
# Shared cyclic payload: byte CONTENT is never verified by the pipeline
# (sizes are), so one 4 MB pattern can stand in for any file size.
_PAYLOAD = (b"destrucyion-loadtest!" * (4 * MB // 21 + 1))[: 4 * MB]


def _slice(pos: int, n: int) -> bytes:
    """n payload bytes starting at absolute position pos (cyclic)."""
    start = pos % len(_PAYLOAD)
    first = _PAYLOAD[start:start + n]
    if len(first) == n:
        return first
    reps = (n - len(first)) // len(_PAYLOAD) + 1
    return (first + _PAYLOAD * reps)[:n]


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


@dataclass
class AccountState:
    """Live per-account view for the dashboard."""

    account_id: int
    planned: int = 0
    done: int = 0
    failed: int = 0
    phase: str = "WAIT"  # WAIT | DOWN | UP
    current_total: int = 0
    current_done: int = 0
    total_bytes: int = 0  # down + up, cumulative — sampled for rate
    last_sample_bytes: int = 0
    last_sample_at: float = 0.0
    rate_bps: float = 0.0


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

    def __init__(self, payload_size: int, msg_id: int = 1) -> None:
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
        self.id = msg_id
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
    """Serves videos of per-message sizes at a simulated rate.

    ``sizes`` maps message id -> file size in bytes; the processor's
    link for a request encodes the message id, so each queued request
    fetches its own planned size.
    """

    def __init__(self, stats: Stats, state: AccountState,
                 sizes: dict[int, int], download_bps: float,
                 upload_bps: float) -> None:
        self._stats = stats
        self._state = state
        self._sizes = sizes
        self._messages: dict[int, FakeMessage] = {}
        self._download_bps = download_bps
        self._upload_bps = upload_bps

    def _message_for(self, msg_id: int) -> FakeMessage:
        if msg_id not in self._messages:
            self._messages[msg_id] = FakeMessage(self._sizes[msg_id], msg_id)
        return self._messages[msg_id]

    async def get_messages(self, peer, ids=None):
        message = self._message_for(int(ids))
        st = self._state
        st.phase = "DOWN"
        st.current_total = self._sizes[int(ids)]
        st.current_done = 0
        return message

    async def _pace(self, nbytes: int, bps: float) -> None:
        if bps > 0:
            await asyncio.sleep(nbytes / bps)

    def iter_download(self, source, *, offset=0, stride=None,
                      chunk_size=None, request_size=None):
        stats, st = self._stats, self._state
        total = st.current_total
        step = request_size or CHUNK
        bps = self._download_bps

        async def _gen():
            stats.active_streams += 1
            stats.max_streams = max(stats.max_streams, stats.active_streams)
            try:
                pos = offset
                while pos < total:
                    end = min(pos + step, total)
                    await self._pace(end - pos, bps)
                    stats.bytes_down += end - pos
                    st.current_done += end - pos
                    st.total_bytes += end - pos
                    yield _slice(pos, end - pos)
                    pos = pos + stride if stride else end
            finally:
                stats.active_streams -= 1

        return _gen()

    async def download_media(self, source, *, file=None, thumb=None,
                             progress_callback=None, **kwargs):
        if thumb is not None:
            return None  # no thumbnail in the fake world
        stats, st = self._stats, self._state
        total = st.current_total
        dest = file
        if dest is None:
            return None
        if os.path.isdir(dest):
            dest = os.path.join(dest, "loadtest.mp4")
        stats.active_streams += 1
        stats.max_streams = max(stats.max_streams, stats.active_streams)
        try:
            pos = 0
            with open(dest, "wb") as fh:
                while pos < total:
                    end = min(pos + CHUNK, total)
                    await self._pace(end - pos, self._download_bps)
                    fh.write(_slice(pos, end - pos))
                    stats.bytes_down += end - pos
                    st.current_done += end - pos
                    st.total_bytes += end - pos
                    if progress_callback is not None:
                        progress_callback(end, total)
                    pos = end
            return dest
        finally:
            stats.active_streams -= 1

    async def send_file(self, **kwargs):
        stats, st = self._stats, self._state
        path = kwargs.get("file")
        size = os.path.getsize(path) if path and os.path.exists(path) else 0
        st.phase = "UP"
        st.current_total = size
        st.current_done = 0
        stats.active_uploads += 1
        stats.max_uploads = max(stats.max_uploads, stats.active_uploads)
        try:
            remaining = size
            while remaining > 0:
                step = min(CHUNK, remaining)
                await self._pace(step, self._upload_bps)
                stats.bytes_up += step
                st.current_done += step
                st.total_bytes += step
                remaining -= step
        finally:
            stats.active_uploads -= 1
        stats.uploads += 1
        stats.sent_ids += 1
        st.phase = "WAIT"
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


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def _bar(done: int, total: int, width: int = 18) -> str:
    if total <= 0:
        return " " * width
    filled = min(width, int(width * min(done, total) / total))
    return "█" * filled + "░" * (width - filled)


def _symbol(st: AccountState) -> str:
    if st.failed > 0 and st.done + st.failed >= st.planned:
        return "✗"
    if st.done + st.failed >= st.planned and st.planned > 0:
        return "✓"
    if st.phase == "DOWN":
        return "D"
    if st.phase == "UP":
        return "U"
    return "·"


def _render(states: list[AccountState], stats: Stats, *, started: float,
            total_requests: int, db_counts: dict) -> str:
    now = time.monotonic()
    elapsed = now - started
    done_n = sum(s.done for s in states)
    failed_n = sum(s.failed for s in states)
    lines = []
    lines.append("═" * 78)
    lines.append(
        f" LOAD TEST  elapsed {elapsed:6.1f}s   "
        f"done {done_n}/{total_requests}   failed {failed_n}   "
        f"queue pending {db_counts.get('pending', '?')}  "
        f"processing {db_counts.get('processing', '?')}"
    )
    lines.append(
        f" traffic    ↓ {stats.bytes_down / MB:9.1f} MB   ↑ {stats.bytes_up / MB:9.1f} MB   "
        f"streams {stats.active_streams} (max {stats.max_streams})   "
        f"uploads {stats.active_uploads} (max {stats.max_uploads})"
    )
    lines.append("─" * 78)
    lines.append(" users:  ✓ done   D downloading   U uploading   · waiting   ✗ failed")
    wall = [_symbol(s) for s in states]
    for i in range(0, len(wall), 50):
        lines.append(" " + " ".join(wall[i:i + 50]))
    lines.append("─" * 78)
    active = [s for s in states if s.phase in ("DOWN", "UP")]
    active.sort(key=lambda s: s.account_id)
    for s in active[:14]:
        if s.last_sample_at:
            dt = max(now - s.last_sample_at, 0.001)
            s.rate_bps = 0.6 * s.rate_bps + 0.4 * (
                (s.total_bytes - s.last_sample_bytes) / dt
            )
        s.last_sample_bytes = s.total_bytes
        s.last_sample_at = now
        shown_done = min(s.current_done, s.current_total)
        pct = (100 * shown_done / s.current_total) if s.current_total else 0
        lines.append(
            f" acc {s.account_id:>4}  {s.phase:<4} "
            f"{shown_done / MB:7.1f}/{s.current_total / MB:6.1f} MB  "
            f"[{_bar(s.current_done, s.current_total)}] {pct:5.1f}%  "
            f"{s.rate_bps / MB:5.1f} MB/s  (selesai {s.done}/{s.planned})"
        )
    if len(active) > 14:
        lines.append(f" …dan {len(active) - 14} user aktif lainnya")
    if not active:
        lines.append(" (tidak ada transfer aktif saat ini)")
    lines.append("═" * 78)
    return "\n".join(lines)


async def _dashboard(states, stats, *, started, total_requests, live: bool,
                     db_counts: dict, stop: asyncio.Event) -> None:
    if live:
        sys.stdout.write("\033[?25l")  # hide cursor
        try:
            while not stop.is_set():
                frame = _render(states, stats, started=started,
                                total_requests=total_requests, db_counts=db_counts)
                sys.stdout.write("\033[H\033[2J" + frame + "\n")
                sys.stdout.flush()
                await asyncio.sleep(0.5)
        finally:
            sys.stdout.write("\033[?25h\033[H\033[2J")
            sys.stdout.flush()
    else:
        while not stop.is_set():
            done_n = sum(s.done for s in states)
            failed_n = sum(s.failed for s in states)
            elapsed = time.monotonic() - started
            print(
                f"[{elapsed:7.1f}s] done {done_n}/{total_requests} failed {failed_n} "
                f"pending {db_counts.get('pending', '?')} "
                f"↓ {stats.bytes_down / MB:.0f} MB ↑ {stats.bytes_up / MB:.0f} MB "
                f"streams {stats.active_streams} uploads {stats.active_uploads}",
                flush=True,
            )
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

def _parse_range(text: str) -> tuple[int, int]:
    if "-" in text:
        lo, hi = text.split("-", 1)
        return int(lo), int(hi)
    return int(text), int(text)


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

    rng = random.Random(args.seed)
    settings = get_settings()

    # Temp-space preflight: every in-flight save holds one full temp
    # copy of the file (the downloader preallocates it), so the temp
    # dir needs concurrency x largest file free — running out of disk
    # mid-transfer is exactly the failure this test would otherwise
    # report confusingly. (Same math applies to a production VPS.)
    import shutil
    import tempfile

    in_flight = min(settings.link_request_max_concurrency, args.accounts)
    needed_mb = in_flight * args.file_max_mb * 1.5
    free_mb = shutil.disk_usage(tempfile.gettempdir()).free / 1e6
    print(f"temp dir: {tempfile.gettempdir()}  free={free_mb:.0f} MB  "
          f"needed~{needed_mb:.0f} MB ({in_flight} in flight x "
          f"{args.file_max_mb} MB x1.5)")
    if free_mb < needed_mb:
        print("ERROR: not enough temp space for this run — lower --accounts/"
              "--file-max-mb, lower LINK_REQUEST_MAX_CONCURRENCY, or point "
              "TMPDIR at a bigger disk.")
        return 2

    print(f"database: {args.database_url}")
    print(f"accounts={args.accounts} requests/account={args.requests_per_account} "
          f"files {args.file_min_mb}-{args.file_max_mb} MB "
          f"download={args.download_mbps} MB/s/conn upload={args.upload_mbps} MB/s "
          f"(jittered per account) queue_concurrency="
          f"{settings.link_request_max_concurrency} download_workers="
          f"{settings.download_workers} seed={args.seed}")

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

    # ---- plan + seed ---------------------------------------------------
    req_lo, req_hi = _parse_range(args.requests_per_account)
    stats = Stats()
    manager = FakeClientManager()
    states: list[AccountState] = []
    states_by_account: dict[int, AccountState] = {}
    request_ids: list[int] = []
    request_account: dict[int, int] = {}
    base_tg_id = 900_000_000 + int(time.time()) % 100_000
    next_msg_id = 1

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
            planned = rng.randint(req_lo, req_hi)
            sizes: dict[int, int] = {}
            st = AccountState(account_id=account.id, planned=planned)
            states.append(st)
            states_by_account[account.id] = st
            jitter = rng.uniform(0.6, 1.6)
            manager.add(
                account.id,
                user.id,
                FakeTelegramClient(
                    stats, st, sizes,
                    download_bps=args.download_mbps * MB * jitter,
                    upload_bps=args.upload_mbps * MB * jitter,
                ),
            )
            for _ in range(planned):
                size = int(rng.uniform(args.file_min_mb, args.file_max_mb) * MB)
                sizes[next_msg_id] = size
                row = await link_repo.create(
                    user_id=user.id,
                    telegram_account_id=account.id,
                    raw_link=f"https://t.me/loadtestchan/{next_msg_id}",
                )
                request_ids.append(row.id)
                request_account[row.id] = account.id
                next_msg_id += 1
        await session.commit()
    total_requests = len(request_ids)
    print(f"seeded {total_requests} pending requests for {args.accounts} accounts")

    # ---- drain the queue with the real processor + live dashboard ------
    processor = LinkRequestProcessor(factory, manager, process_message=_process_message)
    processor._notify_owner = _no_notify.__get__(processor, LinkRequestProcessor)

    db_counts: dict[str, int] = {"pending": total_requests, "processing": 0}
    stop = asyncio.Event()
    started = time.monotonic()
    live = args.live or ((not args.no_live) and sys.stdout.isatty())
    if live:
        print("DASHBOARD LIVE aktif — tampilan diperbarui tiap 0.5 detik "
              "selama test berjalan. Ctrl+C berhenti dengan aman dan "
              "laporan akhir tetap dicetak.", flush=True)
        # The pipeline's INFO logs would shred the dashboard; keep WARNING+.
        import logging

        logging.getLogger().setLevel(logging.WARNING)
    dash = asyncio.create_task(
        _dashboard(states, stats, started=started,
                   total_requests=total_requests, live=live,
                   db_counts=db_counts, stop=stop)
    )

    async def _refresh_counts() -> None:
        async with factory() as session:
            rows = (await session.execute(
                select(LinkRequest.status, func.count())
                .where(LinkRequest.id.in_(request_ids))
                .group_by(LinkRequest.status)
            )).all()
            done_rows = (await session.execute(
                select(LinkRequest.telegram_account_id, LinkRequest.status,
                       func.count())
                .where(LinkRequest.id.in_(request_ids))
                .group_by(LinkRequest.telegram_account_id, LinkRequest.status)
            )).all()
        counts = dict(rows)
        db_counts["pending"] = counts.get(LinkRequestStatus.PENDING, 0)
        db_counts["processing"] = counts.get(LinkRequestStatus.PROCESSING, 0)
        for st in states:
            st.done = 0
            st.failed = 0
        for account_id, status, count in done_rows:
            st = states_by_account.get(account_id)
            if st is None:
                continue
            if status == LinkRequestStatus.DONE:
                st.done = count
            elif status == LinkRequestStatus.FAILED:
                st.failed = count

    async def _counts_loop() -> None:
        while not stop.is_set():
            try:
                await _refresh_counts()
            except Exception:
                pass  # display bookkeeping must never break the drain
            await asyncio.sleep(2)

    counts_task = asyncio.create_task(_counts_loop())

    # Ctrl+C (SIGINT) or a container stop (SIGTERM) must end the run
    # gracefully WITH the final report — not kill the process silently.
    # The handler cancels the drain; the CancelledError is caught below
    # and execution falls through to the report with partial results.
    interrupted = False
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()

    def _request_stop() -> None:
        nonlocal interrupted
        interrupted = True
        if main_task is not None:
            main_task.cancel()

    import signal as _signal

    for _sig in (_signal.SIGINT, _signal.SIGTERM):
        try:
            loop.add_signal_handler(_sig, _request_stop)
        except (NotImplementedError, RuntimeError):
            pass

    deadline = started + args.timeout
    try:
        while time.monotonic() < deadline:
            claimed = await processor.process_pending_once()
            await _refresh_counts()
            remaining = db_counts["pending"] + db_counts["processing"]
            if claimed == 0 and remaining == 0:
                break
            if claimed == 0:
                await asyncio.sleep(0.25)
    except asyncio.CancelledError:
        if not interrupted:
            raise
    finally:
        stop.set()
        await dash
        counts_task.cancel()
        for _sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.remove_signal_handler(_sig)
            except (NotImplementedError, RuntimeError):
                pass
    wall = time.monotonic() - started

    # ---- final report ---------------------------------------------------
    async with factory() as session:
        rows = list((await session.execute(
            select(LinkRequest).where(LinkRequest.id.in_(request_ids))
        )).scalars().all())
    await engine.dispose()

    done = [r for r in rows if r.status == LinkRequestStatus.DONE]
    done_empty = [r for r in done if not r.result_count]
    failed = [r for r in rows if r.status == LinkRequestStatus.FAILED]
    unfinished = [r for r in rows if r.status in (
        LinkRequestStatus.PENDING, LinkRequestStatus.PROCESSING)]
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
    if interrupted:
        print("!! Dihentikan manual (Ctrl+C) — hasil parsial di bawah ini.")
    print(f"requests:        {total_requests}  done={len(done)} "
          f"(empty={len(done_empty)})  failed={len(failed)}  "
          f"unfinished={len(unfinished)}")
    print(f"wall time:       {wall:.1f}s  ({total_requests / max(wall, 0.001):.2f} req/s)")
    print(f"max download streams active at once: {stats.max_streams}")
    print(f"max uploads active at once:          {stats.max_uploads}")
    print(f"bytes downloaded: {stats.bytes_down / MB:.1f} MB   "
          f"uploaded: {stats.bytes_up / MB:.1f} MB   uploads: {stats.uploads}")
    if latencies:
        print(f"per-request latency (queue->done): avg={sum(latencies)/len(latencies):.1f}s "
              f"p50={_pct(0.50):.1f}s p95={_pct(0.95):.1f}s max={latencies[-1]:.1f}s")
    if failed:
        print("first failure:", failed[0].error)
    ok = (len(done) == total_requests and not done_empty
          and stats.uploads == total_requests)
    if interrupted:
        print("verdict: PARTIAL — stopped early by the user")
        return 130
    print("verdict:", "PASS — every request saved exactly once" if ok else "FAIL")
    return 0 if ok else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--accounts", type=int, default=10)
    parser.add_argument("--requests-per-account", default="1",
                        help="'3' or a range like '2-5' (random per account)")
    parser.add_argument("--file-mb", type=float, default=None,
                        help="fixed file size; shorthand for min=max")
    parser.add_argument("--file-min-mb", type=float, default=30)
    parser.add_argument("--file-max-mb", type=float, default=200)
    parser.add_argument("--download-mbps", type=float, default=1.5,
                        help="simulated per-connection download MB/s")
    parser.add_argument("--upload-mbps", type=float, default=3.0,
                        help="simulated per-connection upload MB/s")
    parser.add_argument("--seed", type=int, default=42,
                        help="random seed — same seed, same workload plan")
    parser.add_argument("--no-live", action="store_true",
                        help="plain periodic summaries instead of the live dashboard")
    parser.add_argument("--live", action="store_true",
                        help="force the live dashboard even when stdout is "
                             "not a TTY (e.g. watching via docker logs -f)")
    parser.add_argument("--database-url", default="sqlite+aiosqlite:///./loadtest.db")
    parser.add_argument("--timeout", type=float, default=3600)
    parser.add_argument("--confirm-scratch-db", action="store_true",
                        help="required: confirms the target DB is a scratch DB")
    args = parser.parse_args()
    if args.file_mb is not None:
        args.file_min_mb = args.file_max_mb = args.file_mb
    if not args.confirm_scratch_db:
        parser.error("refusing to run without --confirm-scratch-db "
                     "(see the safety note in this file's docstring)")
    if args.database_url == "sqlite+aiosqlite:///./loadtest.db" and os.path.exists("loadtest.db"):
        os.remove("loadtest.db")  # our own default scratch file
    try:
        sys.exit(asyncio.run(run(args)))
    except KeyboardInterrupt:
        print("\nstopped by user")
        sys.exit(130)


if __name__ == "__main__":
    main()
