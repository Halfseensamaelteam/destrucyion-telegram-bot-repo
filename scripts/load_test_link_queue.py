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

  # the 100-user run on the VPS, inside Docker. NOTE the --entrypoint:
  # the worker service's own entrypoint (docker-entrypoint.sh) IGNORES
  # any command and starts the worker by SERVICE_ROLE, so a plain
  # `docker compose run worker python ...` just boots a worker and the
  # script never runs. Overriding the entrypoint is what actually
  # executes it. Create the scratch DB once first:
  #   docker compose exec postgres createdb -U postgres loadtest
  docker compose run --rm --entrypoint python worker \
      scripts/load_test_link_queue.py --confirm-scratch-db --live \
      --accounts 100 --file-min-mb 30 --file-max-mb 200 \
      --requests-per-account 2-5 \
      --database-url postgresql+asyncpg://postgres:postgres@postgres:5432/loadtest

Mixed-size scenario (size-aware disk admission): two user groups —
many small files, fewer huge ones — against a SIMULATED disk. The
real production DiskLeaseManager is injected with a virtual
free-space probe (base free minus temp bytes held by fake transfers),
and file sizes are simulated (transfers pace/count virtual bytes;
real temp files are 1-byte stubs), so a 22 GB disk and 900 MB videos
run fine on a laptop. The report asserts: virtual free never dropped
below the reserve, the small class finished first, the big class kept
moving during the small flood (no starvation), and bytes balanced
exactly once. Wood's production-shaped case (set a high
LINK_REQUEST_MAX_CONCURRENCY in the environment first):

  docker compose run --rm --entrypoint python worker \
      scripts/load_test_link_queue.py --confirm-scratch-db \
      --small-accounts 400 --small-file-min-mb 10 --small-file-max-mb 80 \
      --big-accounts 100 --big-file-min-mb 300 --big-file-max-mb 900 \
      --virtual-free-mb 22528 --disk-reserve-mb 3072 \
      --database-url postgresql+asyncpg://postgres:postgres@postgres:5432/loadtest

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
class ResourceStats:
    """This process's own resource usage, sampled during the drain.

    Linux /proc based (the harness runs in a Linux container): RSS from
    /proc/self/statm, CPU from /proc/self/stat utime+stime. CPU percent
    is relative to ONE core (100% = one core fully busy).
    """

    samples: int = 0
    rss_peak_mb: float = 0.0
    rss_sum_mb: float = 0.0
    cpu_max_pct: float = 0.0
    cpu_sum_pct: float = 0.0
    cpu_seconds: float = 0.0
    _last_cpu_s: float | None = None
    _last_at: float | None = None

    @staticmethod
    def _read() -> tuple[float, float] | None:
        try:
            with open("/proc/self/statm") as fh:
                statm = fh.read().split()
            rss_mb = int(statm[1]) * os.sysconf("SC_PAGE_SIZE") / MB
            with open("/proc/self/stat") as fh:
                stat = fh.read()
            # comm may contain spaces/parens — fields after the last ')'
            fields = stat[stat.rfind(")") + 2:].split()
            ticks = int(fields[11]) + int(fields[12])  # utime + stime
            cpu_s = ticks / os.sysconf("SC_CLK_TCK")
            return rss_mb, cpu_s
        except Exception:
            return None

    def sample(self) -> None:
        got = self._read()
        if got is None:
            return
        rss_mb, cpu_s = got
        now = time.monotonic()
        self.samples += 1
        self.rss_peak_mb = max(self.rss_peak_mb, rss_mb)
        self.rss_sum_mb += rss_mb
        if self._last_cpu_s is not None and self._last_at is not None:
            dt = now - self._last_at
            if dt > 0:
                pct = 100.0 * (cpu_s - self._last_cpu_s) / dt
                self.cpu_max_pct = max(self.cpu_max_pct, pct)
                self.cpu_sum_pct += pct
        self.cpu_seconds = cpu_s
        self._last_cpu_s = cpu_s
        self._last_at = now

    @property
    def rss_avg_mb(self) -> float:
        return self.rss_sum_mb / self.samples if self.samples else 0.0

    @property
    def cpu_avg_pct(self) -> float:
        timed = max(0, self.samples - 1)
        return self.cpu_sum_pct / timed if timed else 0.0


class VirtualDisk:
    """A simulated temp disk for the mixed-size scenario.

    The lease manager reads free space from here instead of the real
    filesystem: free = base free - temp bytes currently held by fake
    transfers (added when a fake download starts, removed when its
    upload finishes). File SIZES in this mode are pure simulation —
    the fake sequential download writes a 1-byte stub — so a run can
    model a 22 GB-free VPS disk and 900 MB videos on a machine whose
    real temp dir is a few hundred MB. The observed minimum free is
    recorded for the report's reserve invariant check.
    """

    def __init__(self, base_free_mb: float) -> None:
        self.base_free_mb = float(base_free_mb)
        self.active_temp_mb = 0.0
        self.min_free_mb = float(base_free_mb)

    def free_mb(self) -> float:
        free = self.base_free_mb - self.active_temp_mb
        self.min_free_mb = min(self.min_free_mb, free)
        return free

    def add_temp(self, size_bytes: int) -> None:
        self.active_temp_mb += size_bytes / MB
        self.free_mb()  # sample the dip

    def remove_temp(self, size_bytes: int) -> None:
        self.active_temp_mb = max(0.0, self.active_temp_mb - size_bytes / MB)


@dataclass
class AccountState:
    """Live per-account view for the dashboard."""

    account_id: int
    label: int = 0  # 1-based number within THIS run (user-001, user-002…)
    group: str = ""  # mixed scenario only: "small" | "big"
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
                 upload_bps: float, start_delay: float = 0.0,
                 vdisk: VirtualDisk | None = None) -> None:
        self._stats = stats
        self._state = state
        self._sizes = sizes
        self._messages: dict[int, FakeMessage] = {}
        self._download_bps = download_bps
        self._upload_bps = upload_bps
        # Real users do not all start at the same instant: each fake
        # account waits a random stagger once, before its first fetch.
        self._start_delay = start_delay
        self._started = False
        # Mixed scenario only: sizes are simulated against a virtual
        # disk — transfers pace and count VIRTUAL bytes and the temp
        # file on the real disk is a 1-byte stub.
        self._vdisk = vdisk
        self._temp_size_bytes = 0  # virtual temp currently held (this client)
        self._path_sizes: dict[str, int] = {}  # stub path -> virtual size

    def _temp_begin(self, size_bytes: int) -> None:
        if self._vdisk is not None and self._temp_size_bytes == 0:
            self._temp_size_bytes = size_bytes
            self._vdisk.add_temp(size_bytes)

    def _temp_end(self) -> None:
        if self._vdisk is not None and self._temp_size_bytes:
            self._vdisk.remove_temp(self._temp_size_bytes)
            self._temp_size_bytes = 0

    def _message_for(self, msg_id: int) -> FakeMessage:
        if msg_id not in self._messages:
            self._messages[msg_id] = FakeMessage(self._sizes[msg_id], msg_id)
        return self._messages[msg_id]

    async def get_messages(self, peer, ids=None):
        if not self._started:
            self._started = True
            if self._start_delay > 0:
                await asyncio.sleep(self._start_delay)
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
            if self._vdisk is not None:
                # Virtual mode runs with DOWNLOAD_WORKERS=1, so this
                # should never be called; if it ever is, yielding
                # nothing makes the production parallel downloader
                # fail its size check and fall back to the sequential
                # path — exactly the supported behaviour.
                return
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
            if self._vdisk is not None:
                # Virtual mode: pace and count the VIRTUAL bytes, hold
                # them on the virtual disk, but write only a stub — the
                # real temp dir never sees the simulated gigabytes.
                self._temp_begin(total)
                self._path_sizes[dest] = total
                pos = 0
                while pos < total:
                    end = min(pos + CHUNK, total)
                    await self._pace(end - pos, self._download_bps)
                    stats.bytes_down += end - pos
                    st.current_done += end - pos
                    st.total_bytes += end - pos
                    if progress_callback is not None:
                        progress_callback(end, total)
                    pos = end
                with open(dest, "wb") as fh:
                    fh.write(b"\0")
                return dest
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
        if self._vdisk is not None:
            # The real file is a 1-byte stub; the upload is paced and
            # counted by the VIRTUAL size recorded at download time.
            size = self._path_sizes.pop(path, 0) if path else 0
        else:
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
        # The pipeline deletes the temp file right after the upload;
        # in virtual mode this is where the virtual temp is freed.
        self._temp_end()
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

def _bar(done: int, total: int, width: int = 16) -> str:
    if total <= 0:
        return "-" * width
    filled = min(width, int(width * min(done, total) / total))
    return "#" * filled + "-" * (width - filled)


def _status(st: AccountState) -> str:
    finished = st.planned > 0 and st.done + st.failed >= st.planned
    if finished:
        return "FAILED" if st.failed > 0 else "DONE"
    if st.phase == "DOWN":
        return "DOWNLOAD"
    if st.phase == "UP":
        return "UPLOAD"
    return "WAITING"


def _symbol(st: AccountState) -> str:
    return {"DONE": "✓", "FAILED": "✗", "DOWNLOAD": "D",
            "UPLOAD": "U", "WAITING": "·"}[_status(st)]


def _render(states: list[AccountState], stats: Stats, *, started: float,
            total_requests: int, db_counts: dict, view: dict | None = None) -> str:
    now = time.monotonic()
    elapsed = now - started
    done_n = sum(s.done for s in states)
    failed_n = sum(s.failed for s in states)
    lines = []
    lines.append("=" * 95)
    lines.append("  DESTRUCYION — LOAD TEST MONITOR (fake clients, pipeline asli)")
    lines.append("=" * 95)
    lines.append(
        f"  elapsed {elapsed:7.1f}s    selesai {done_n}/{total_requests}    "
        f"gagal {failed_n}    antrean: pending {db_counts.get('pending', '?')}  "
        f"processing {db_counts.get('processing', '?')}"
    )
    lines.append(
        f"  traffic  download {stats.bytes_down / MB:9.1f} MB   "
        f"upload {stats.bytes_up / MB:9.1f} MB    "
        f"stream aktif {stats.active_streams} (max {stats.max_streams})   "
        f"upload aktif {stats.active_uploads} (max {stats.max_uploads})"
    )
    lines.append("-" * 95)
    lines.append("  PETA USER:  [v] selesai   [D] download   [U] upload   [.] menunggu   [x] gagal")
    wall = [_symbol(s).replace("✓", "v").replace("✗", "x") for s in states]
    for i in range(0, len(wall), 50):
        lines.append("  " + " ".join(wall[i:i + 50]))
    lines.append("-" * 95)

    # Full user table in a scrollable viewport. Default mode follows
    # the action (viewport sits on the page with active transfers);
    # any arrow key switches to manual scrolling and the view stays
    # exactly where the user put it — nothing auto-rotates.
    # Border, header and rows are all built from ONE column-width
    # table, so the pipes always line up.
    columns = [("USER", 9), ("STATUS", 10), ("SIZE", 11),
               ("PROCESS", 25), ("SPEED", 11), ("SAVED", 8)]

    def _row(cells: list[str]) -> str:
        return "  |" + "|".join(
            f" {cell:<{width}} " for cell, (_, width) in zip(cells, columns)
        ) + "|"

    border = "  +" + "+".join("-" * (width + 2) for _, width in columns) + "+"
    ordered = sorted(states, key=lambda s: s.label)
    page_size = 20
    if view is None:
        view = {"offset": 0, "follow": True}
    if view["follow"]:
        first_active = next(
            (i for i, s in enumerate(ordered)
             if _status(s) in ("DOWNLOAD", "UPLOAD")),
            0,
        )
        view["offset"] = (first_active // page_size) * page_size
    offset = max(0, min(view["offset"], max(0, len(ordered) - 1)))
    view["offset"] = offset
    rows = ordered[offset:offset + page_size]
    mode = "IKUTI AKTIF" if view["follow"] else "SCROLL MANUAL"
    lines.append(
        f"  TABEL USER — baris {offset + 1}-{offset + len(rows)} "
        f"dari {len(ordered)}   mode: {mode}"
    )
    lines.append(border)
    lines.append(_row([title for title, _ in columns]))
    lines.append(border)
    for s in rows:
        if s.last_sample_at:
            dt = max(now - s.last_sample_at, 0.001)
            s.rate_bps = 0.6 * s.rate_bps + 0.4 * (
                (s.total_bytes - s.last_sample_bytes) / dt
            )
        s.last_sample_bytes = s.total_bytes
        s.last_sample_at = now
        status = _status(s)
        if status == "DONE":
            pct = 100.0
            bar = _bar(1, 1)
        elif status in ("DOWNLOAD", "UPLOAD") and s.current_total:
            shown = min(s.current_done, s.current_total)
            pct = 100 * shown / s.current_total
            bar = _bar(shown, s.current_total)
        else:
            pct = 0.0
            bar = _bar(0, 1)
        size_mb = s.current_total / MB if s.current_total else 0.0
        speed = f"{s.rate_bps / MB:.1f} MB/s" if status in ("DOWNLOAD", "UPLOAD") else "-"
        lines.append(_row([
            f"user-{s.label:03d}",
            status,
            f"{size_mb:6.1f} MB",
            f"[{bar}] {pct:5.1f}%",
            speed,
            f"{s.done}/{s.planned}",
        ]))
    lines.append(border)
    lines.append("=" * 95)
    lines.append(
        "  [↑]/[↓] geser 1 baris   [PgUp]/[PgDn] geser 1 halaman   "
        "[f] ikuti aktif   [q] / Ctrl+C berhenti (laporan tetap dicetak)"
    )
    return "\n".join(lines)


async def _dashboard(states, stats, *, started, total_requests, live: bool,
                     db_counts: dict, stop: asyncio.Event, on_quit=None) -> None:
    if live:
        view = {"offset": 0, "follow": True}
        # Keyboard control: raw-mode stdin, arrow keys scroll the table
        # viewport (no auto-rotation). Disabled silently when stdin is
        # not a TTY (follow mode then simply stays on).
        loop = asyncio.get_running_loop()
        fd = None
        old_term = None
        keybuf = bytearray()

        def _on_key() -> None:
            try:
                data = os.read(fd, 64)
            except OSError:
                return
            if not data:
                return
            keybuf.extend(data)
            while keybuf:
                head3 = bytes(keybuf[:3])
                head4 = bytes(keybuf[:4])
                if head3 == b"\x1b[A" or keybuf[:1] == b"k":
                    n = 3 if head3 == b"\x1b[A" else 1
                    del keybuf[:n]
                    view["follow"] = False
                    view["offset"] -= 1
                elif head3 == b"\x1b[B" or keybuf[:1] == b"j":
                    n = 3 if head3 == b"\x1b[B" else 1
                    del keybuf[:n]
                    view["follow"] = False
                    view["offset"] += 1
                elif head4 == b"\x1b[5~":
                    del keybuf[:4]
                    view["follow"] = False
                    view["offset"] -= 20
                elif head4 == b"\x1b[6~":
                    del keybuf[:4]
                    view["follow"] = False
                    view["offset"] += 20
                elif keybuf[:1] == b"f":
                    del keybuf[:1]
                    view["follow"] = True
                elif keybuf[:1] == b"q":
                    del keybuf[:1]
                    if on_quit is not None:
                        on_quit()
                elif keybuf[:1] == b"\x1b" and len(keybuf) < 4:
                    break  # partial escape sequence — wait for the rest
                else:
                    del keybuf[:1]
            view["offset"] = max(0, view["offset"])

        try:
            import termios
            import tty

            fd = sys.stdin.fileno()
            old_term = termios.tcgetattr(fd)
            tty.setcbreak(fd)
            loop.add_reader(fd, _on_key)
        except Exception:
            fd = None
            old_term = None

        sys.stdout.write("\033[?25l")  # hide cursor
        try:
            while not stop.is_set():
                frame = _render(states, stats, started=started,
                                total_requests=total_requests,
                                db_counts=db_counts, view=view)
                sys.stdout.write("\033[H\033[2J" + frame + "\n")
                sys.stdout.flush()
                await asyncio.sleep(0.5)
        finally:
            if fd is not None and old_term is not None:
                import termios

                loop.remove_reader(fd)
                termios.tcsetattr(fd, termios.TCSADRAIN, old_term)
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

    mixed = (args.small_accounts + args.big_accounts) > 0
    if mixed:
        # Virtual-disk mode simulates file sizes, so the parallel
        # downloader — which writes REAL preallocated files — must not
        # run: DOWNLOAD_WORKERS=1 is the supported switch that keeps
        # every transfer on the sequential path (whose fake writes a
        # stub). Set it before settings are first read.
        os.environ["DOWNLOAD_WORKERS"] = "1"
        get_settings.cache_clear()

    rng = random.Random(args.seed)
    settings = get_settings()

    vdisk: VirtualDisk | None = None
    if mixed:
        vdisk = VirtualDisk(args.virtual_free_mb)
        usable_mb = max(0.0, args.virtual_free_mb - args.disk_reserve_mb)
        print(f"MIXED SCENARIO (virtual disk): base free "
              f"{args.virtual_free_mb:.0f} MB, reserve "
              f"{args.disk_reserve_mb:.0f} MB -> usable {usable_mb:.0f} MB; "
              f"big class > {args.big_file_mb:.0f} MB, pool "
              f"{args.big_pool_percent:.0f}% of usable, guarantee "
              f"{args.big_guarantee_percent:.0f}% fenced from small")
        print(f"  small: {args.small_accounts} accounts x "
              f"{args.small_file_min_mb}-{args.small_file_max_mb} MB; "
              f"big: {args.big_accounts} accounts x "
              f"{args.big_file_min_mb}-{args.big_file_max_mb} MB; "
              f"real temp files are 1-byte stubs (sizes simulated)")

    # Temp-space preflight: every in-flight save holds one full temp
    # copy of the file (the downloader preallocates it), so the temp
    # dir needs concurrency x largest file free — running out of disk
    # mid-transfer is exactly the failure this test would otherwise
    # report confusingly. (Same math applies to a production VPS.)
    import shutil
    import tempfile

    cap = settings.link_request_max_concurrency
    free_mb = shutil.disk_usage(tempfile.gettempdir()).free / 1e6
    if mixed:
        # The real-temp preflight below does not apply: transfers hold
        # VIRTUAL temp on the virtual disk, and the real temp dir only
        # ever holds 1-byte stubs. The disk invariant is enforced by
        # the lease manager against the virtual disk and asserted in
        # the final report instead.
        print(f"temp dir: {tempfile.gettempdir()} (stubs only in mixed mode)")
    elif cap <= 0:
        # Budget-only mode: the processor's disk guard sizes slots from
        # free space, so the worst case IS the disk — derive the
        # effective in-flight count from it instead of blocking a run
        # the guard would have kept safe.
        in_flight = min(
            args.accounts, max(1, int(free_mb // (args.file_max_mb * 1.5)))
        )
        print(f"budget-only mode: effective in-flight from disk "
              f"= {in_flight}")
    else:
        in_flight = min(cap, args.accounts)
    if not mixed:
        needed_mb = in_flight * args.file_max_mb * 1.5
        print(f"temp dir: {tempfile.gettempdir()}  free={free_mb:.0f} MB  "
              f"needed~{needed_mb:.0f} MB ({in_flight} in flight x "
              f"{args.file_max_mb} MB x1.5)")
        if free_mb < needed_mb:
            print("ERROR: not enough temp space for this run — lower "
                  "--accounts/--file-max-mb, lower "
                  "LINK_REQUEST_MAX_CONCURRENCY, or point TMPDIR at a "
                  "bigger disk.")
            return 2

    print(f"database: {args.database_url}")
    if mixed:
        total_accounts = args.small_accounts + args.big_accounts
        print(f"accounts={total_accounts} (small {args.small_accounts} + "
              f"big {args.big_accounts}) "
              f"requests/account={args.requests_per_account} "
              f"download={args.download_mbps} MB/s/conn "
              f"upload={args.upload_mbps} MB/s (jittered per account) "
              f"queue_concurrency={settings.link_request_max_concurrency} "
              f"download_workers={settings.download_workers} "
              f"seed={args.seed}")
    else:
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

    # Purge leftovers of previous harness runs from this scratch DB:
    # otherwise account ids keep climbing across runs (user-301,
    # user-401, ...) and yesterday's unfinished fake rows eat claim
    # slots. Harness data is identifiable — queue rows point at
    # t.me/loadtestchan and fake users carry telegram_user_id >= 9e8.
    async with factory() as session:
        from sqlalchemy import delete as sa_delete

        from app.db.models.link_request import LinkRequest as _LR
        from app.db.models.media_record import MediaRecord as _MR
        from app.db.models.telegram_account import TelegramAccount as _TA
        from app.db.models.user import User as _U

        fake_users = select(_U.id).where(_U.telegram_user_id >= 900_000_000)
        fake_accounts = select(_TA.id).where(_TA.user_id.in_(fake_users))
        await session.execute(
            sa_delete(_LR).where(_LR.raw_link.like("https://t.me/loadtestchan/%"))
        )
        await session.execute(
            sa_delete(_MR).where(_MR.telegram_account_id.in_(fake_accounts))
        )
        await session.execute(sa_delete(_TA).where(_TA.id.in_(fake_accounts)))
        await session.execute(sa_delete(_U).where(_U.id.in_(fake_users)))
        await session.commit()

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
        # Pass 1: accounts, clients, and each account's size plan.
        # Mixed mode seeds two groups (small-file users, big-file
        # users); classic mode is a single anonymous group.
        if mixed:
            group_specs = [
                ("small", args.small_accounts,
                 args.small_file_min_mb, args.small_file_max_mb),
                ("big", args.big_accounts,
                 args.big_file_min_mb, args.big_file_max_mb),
            ]
        else:
            group_specs = [("", args.accounts,
                            args.file_min_mb, args.file_max_mb)]
        plans: list[tuple[int, int, dict[int, int], list[int]]] = []
        acct_index = 0
        for group_name, group_count, size_lo, size_hi in group_specs:
            for _ in range(group_count):
                i = acct_index
                acct_index += 1
                user = await user_repo.create(telegram_user_id=base_tg_id + i)
                account = await account_repo.create(
                    user_id=user.id,
                    phone_masked=f"+628****{i:04d}",
                    status=TelegramAccountStatus.ACTIVE,
                )
                planned = rng.randint(req_lo, req_hi)
                sizes: dict[int, int] = {}
                msg_ids: list[int] = []
                for _ in range(planned):
                    sizes[next_msg_id] = int(rng.uniform(size_lo, size_hi) * MB)
                    msg_ids.append(next_msg_id)
                    next_msg_id += 1
                st = AccountState(
                    account_id=account.id, label=i + 1,
                    planned=planned, group=group_name,
                )
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
                        start_delay=rng.uniform(0, args.stagger_seconds),
                        vdisk=vdisk,
                    ),
                )
                plans.append((user.id, account.id, sizes, msg_ids))
        # Pass 2: create the queue rows ROUND-ROBIN across accounts
        # (everyone's 1st request first, then everyone's 2nd, ...).
        # The processor claims oldest-first, so block-seeding one
        # account's whole queue first would let the first ~3 accounts
        # hog the opening batches behind the per-account lock while
        # user 150 never starts; interleaving makes the first batch
        # span 10 different users, like real arrivals.
        max_rounds = max((len(p[3]) for p in plans), default=0)
        for r in range(max_rounds):
            round_accounts = [p for p in plans if len(p[3]) > r]
            rng.shuffle(round_accounts)
            for user_id, account_id, _sizes, msg_ids in round_accounts:
                row = await link_repo.create(
                    user_id=user_id,
                    telegram_account_id=account_id,
                    raw_link=f"https://t.me/loadtestchan/{msg_ids[r]}",
                )
                request_ids.append(row.id)
                request_account[row.id] = account_id
        await session.commit()
    total_requests = len(request_ids)
    expected_bytes = sum(sum(p[2].values()) for p in plans)
    print(f"seeded {total_requests} pending requests for {len(states)} accounts"
          f" ({expected_bytes / MB:.0f} MB of media)")

    # ---- drain the queue with the real processor + live dashboard ------
    # Neutralize the operator admin notification: when the environment
    # carries a real BOT_TOKEN + ADMIN_NOTIFY_CHAT_ID (e.g. the trial
    # stack's .env), every save would otherwise try to upload the whole
    # fake file to Telegram via the real Bot API — 3 retries x 300 s
    # timeouts — stalling the pipeline for minutes per request (this
    # actually happened: 3 fake uploads done, 0 requests DONE in 117 s).
    # Admin notify is an operator side-channel, not the pipeline under
    # test. (events._process_message imports it per call, so replacing
    # the module attribute is enough.)
    import app.telegram.admin_notify as _admin_notify_module

    async def _noop_admin_notify(**kwargs) -> None:
        return None

    _admin_notify_module.notify_admin_of_capture = _noop_admin_notify

    if mixed:
        # The processor leases against the VIRTUAL disk: this is the
        # exact production DiskLeaseManager, only its free-space probe
        # reads the simulation (base free minus active fake temp).
        from app.core.disk_lease import DiskLeaseManager

        lease_manager = DiskLeaseManager(
            reserve_mb=args.disk_reserve_mb,
            big_file_mb=args.big_file_mb,
            big_pool_percent=args.big_pool_percent,
            big_guarantee_percent=args.big_guarantee_percent,
            free_mb_reader=vdisk.free_mb,
        )
        processor = LinkRequestProcessor(
            factory, manager, process_message=_process_message,
            lease_manager=lease_manager,
        )
    else:
        # Classic mode measures the queue/concurrency pipeline exactly
        # as it behaved before disk admission existed, so the lease
        # manager is injected DISABLED (reserve 0). Otherwise the
        # settings default (3072 MB reserve) would fail every request
        # on any machine whose real temp free is under ~4 GB — the
        # admission system has its own scenario (mixed mode) and unit
        # tests; in production it is driven by the real settings.
        from app.core.disk_lease import DiskLeaseManager

        processor = LinkRequestProcessor(
            factory, manager, process_message=_process_message,
            lease_manager=DiskLeaseManager(
                reserve_mb=0, big_file_mb=100, big_pool_percent=30,
                big_guarantee_percent=15, free_mb_reader=lambda: None,
            ),
        )
    processor._notify_owner = _no_notify.__get__(processor, LinkRequestProcessor)

    db_counts: dict[str, int] = {"pending": total_requests, "processing": 0}
    stop = asyncio.Event()
    started = time.monotonic()
    live = args.live or ((not args.no_live) and sys.stdout.isatty())
    if live:
        print("DASHBOARD LIVE aktif — tampilan diperbarui tiap 0.5 detik "
              "selama test berjalan. Ctrl+C berhenti dengan aman dan "
              "laporan akhir tetap dicetak.", flush=True)
        # The pipeline's INFO logs would shred the dashboard; keep
        # WARNING+. The app logs through structlog filtering wrappers,
        # which ignore the stdlib root level — reconfigure the wrapper
        # too, or lines like saved_messages_transfer_progress leak
        # straight through the table.
        import logging

        logging.getLogger().setLevel(logging.WARNING)
        try:
            import structlog

            structlog.configure(
                wrapper_class=structlog.make_filtering_bound_logger(
                    logging.WARNING
                )
            )
        except Exception:
            pass
    # Ctrl+C (SIGINT), a container stop (SIGTERM), or pressing [q] in
    # the dashboard must end the run gracefully WITH the final report —
    # not kill the process silently. The handler cancels the drain; the
    # CancelledError is caught below and execution falls through to the
    # report with partial results.
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

    dash = asyncio.create_task(
        _dashboard(states, stats, started=started,
                   total_requests=total_requests, live=live,
                   db_counts=db_counts, stop=stop, on_quit=_request_stop)
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

    # Mixed-scenario timeline evidence for the report's invariants:
    # when each class finished, and whether the big class was visibly
    # moving (a transfer active, or one already done) while the small
    # flood was still flowing — the no-starvation proof.
    timeline = {
        "small_done_at": None,
        "big_done_at": None,
        "big_progress_during_small_flood": False,
    }

    def _sample_timeline() -> None:
        if not mixed:
            return
        elapsed = time.monotonic() - started
        small_states = [s for s in states if s.group == "small"]
        big_states = [s for s in states if s.group == "big"]
        small_total = sum(s.planned for s in small_states)
        small_done = sum(s.done for s in small_states)
        big_total = sum(s.planned for s in big_states)
        big_done = sum(s.done for s in big_states)
        if small_total and small_done >= small_total \
                and timeline["small_done_at"] is None:
            timeline["small_done_at"] = elapsed
        if big_total and big_done >= big_total \
                and timeline["big_done_at"] is None:
            timeline["big_done_at"] = elapsed
        if small_total and small_done < small_total:
            if big_done > 0 or any(
                s.phase in ("DOWN", "UP") for s in big_states
            ):
                timeline["big_progress_during_small_flood"] = True

    async def _counts_loop() -> None:
        while not stop.is_set():
            try:
                await _refresh_counts()
                _sample_timeline()
            except Exception:
                pass  # display bookkeeping must never break the drain
            await asyncio.sleep(2)

    counts_task = asyncio.create_task(_counts_loop())

    resources = ResourceStats()

    async def _resource_loop() -> None:
        while not stop.is_set():
            resources.sample()
            await asyncio.sleep(0.5)

    resource_task = asyncio.create_task(_resource_loop())

    # Drive the processor exactly like production does (worker/main.py):
    # its continuous run_forever supervisor, not one-shot batch passes —
    # the test must measure the real admission behaviour.
    processor_task = asyncio.create_task(processor.run_forever())
    deadline = started + args.timeout
    try:
        while time.monotonic() < deadline:
            await _refresh_counts()
            _sample_timeline()
            remaining = db_counts["pending"] + db_counts["processing"]
            if remaining == 0:
                break
            await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        if not interrupted:
            raise
    finally:
        processor_task.cancel()
        await processor.shutdown()
        stop.set()
        await dash
        counts_task.cancel()
        resource_task.cancel()
        for _sig in (_signal.SIGINT, _signal.SIGTERM):
            try:
                loop.remove_signal_handler(_sig)
            except (NotImplementedError, RuntimeError):
                pass
    wall = time.monotonic() - started
    try:
        await _refresh_counts()
        _sample_timeline()
    except Exception:
        pass  # the row-level report below is authoritative anyway

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
    if resources.samples:
        print(f"resources (proses ini): RAM peak {resources.rss_peak_mb:.1f} MB   "
              f"RAM avg {resources.rss_avg_mb:.1f} MB   "
              f"CPU max {resources.cpu_max_pct:.0f}%   "
              f"CPU avg {resources.cpu_avg_pct:.0f}%   "
              f"(total CPU {resources.cpu_seconds:.1f}s, 1 core = 100%)")
    if latencies:
        print(f"per-request latency (queue->done): avg={sum(latencies)/len(latencies):.1f}s "
              f"p50={_pct(0.50):.1f}s p95={_pct(0.95):.1f}s max={latencies[-1]:.1f}s")
    if failed:
        print("first failure:", failed[0].error)
    mixed_ok = True
    if mixed:
        group_by_account = {s.account_id: s.group for s in states}

        def _class_rows(name):
            return [r for r in rows
                    if group_by_account.get(request_account[r.id]) == name]

        small_rows = _class_rows("small")
        big_rows = _class_rows("big")
        small_done_n = sum(
            1 for r in small_rows if r.status == LinkRequestStatus.DONE)
        big_done_n = sum(
            1 for r in big_rows if r.status == LinkRequestStatus.DONE)
        reserve_kept = vdisk.min_free_mb >= args.disk_reserve_mb - 1e-9
        bytes_balanced = (
            stats.bytes_down == expected_bytes
            and stats.bytes_up == expected_bytes
        )
        small_first = (
            timeline["small_done_at"] is not None
            and timeline["big_done_at"] is not None
            and timeline["small_done_at"] <= timeline["big_done_at"]
        )
        print("\n---- mixed scenario (virtual disk) ----")
        print(f"virtual disk: base free {vdisk.base_free_mb:.0f} MB, "
              f"reserve {args.disk_reserve_mb:.0f} MB, "
              f"min free observed {vdisk.min_free_mb:.0f} MB "
              f"-> reserve kept: {'YES' if reserve_kept else 'NO'}")
        print(f"small class: {small_done_n}/{len(small_rows)} done"
              + (f", all done at {timeline['small_done_at']:.1f}s"
                 if timeline["small_done_at"] is not None else ""))
        print(f"big class:   {big_done_n}/{len(big_rows)} done"
              + (f", all done at {timeline['big_done_at']:.1f}s"
                 if timeline["big_done_at"] is not None else ""))
        print(f"big progress during small flood (no starvation): "
              f"{'YES' if timeline['big_progress_during_small_flood'] else 'NO'}")
        print(f"small class finished before big class: "
              f"{'YES' if small_first else 'NO'}")
        print(f"bytes balanced (down == up == planned "
              f"{expected_bytes / MB:.0f} MB): "
              f"{'YES' if bytes_balanced else 'NO'}")
        mixed_ok = (
            reserve_kept and bytes_balanced
            and small_done_n == len(small_rows)
            and big_done_n == len(big_rows)
            and (timeline["big_progress_during_small_flood"]
                 or not small_rows or not big_rows)
            and (small_first or not small_rows or not big_rows)
        )
    ok = (len(done) == total_requests and not done_empty
          and stats.uploads == total_requests and mixed_ok)
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
    parser.add_argument("--stagger-seconds", type=float, default=8.0,
                        help="each account starts within this random window, "
                             "so users do not all begin at the same instant")
    parser.add_argument("--no-live", action="store_true",
                        help="plain periodic summaries instead of the live dashboard")
    parser.add_argument("--live", action="store_true",
                        help="force the live dashboard even when stdout is "
                             "not a TTY (e.g. watching via docker logs -f)")
    parser.add_argument("--database-url", default="sqlite+aiosqlite:///./loadtest.db")
    parser.add_argument("--timeout", type=float, default=3600)
    # Mixed-size scenario (size-aware disk admission): two user groups
    # on a SIMULATED disk. Activated when either group count is > 0;
    # --accounts/--file-* are then ignored.
    parser.add_argument("--small-accounts", type=int, default=0,
                        help="mixed scenario: users saving small files")
    parser.add_argument("--small-file-min-mb", type=float, default=10)
    parser.add_argument("--small-file-max-mb", type=float, default=80)
    parser.add_argument("--big-accounts", type=int, default=0,
                        help="mixed scenario: users saving big files")
    parser.add_argument("--big-file-min-mb", type=float, default=300)
    parser.add_argument("--big-file-max-mb", type=float, default=900)
    parser.add_argument("--virtual-free-mb", type=float, default=22528,
                        help="mixed scenario: simulated free temp disk "
                             "at start (default ~ the production VPS's "
                             "22 GB free)")
    parser.add_argument("--disk-reserve-mb", type=float, default=3072,
                        help="mixed scenario: LINK_REQUEST_DISK_RESERVE_MB "
                             "equivalent for the injected lease manager")
    parser.add_argument("--big-file-mb", type=float, default=100,
                        help="mixed scenario: big-class threshold (MB)")
    parser.add_argument("--big-pool-percent", type=float, default=30,
                        help="mixed scenario: big-class pool, % of usable")
    parser.add_argument("--big-guarantee-percent", type=float, default=15,
                        help="mixed scenario: big guarantee fenced from "
                             "small leases, % of usable")
    parser.add_argument("--confirm-scratch-db", action="store_true",
                        help="required: confirms the target DB is a scratch DB")
    args = parser.parse_args()
    if args.file_mb is not None:
        args.file_min_mb = args.file_max_mb = args.file_mb
    if (args.small_accounts + args.big_accounts) > 0 and (
        args.virtual_free_mb <= args.disk_reserve_mb + 1024
    ):
        parser.error("--virtual-free-mb must exceed --disk-reserve-mb "
                     "by more than 1024 MB, or no file can ever be "
                     "admitted on the virtual disk")
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
