"""app/core/disk_lease.py — size-aware temp-disk admission (leases).

Why this exists
---------------
The link-request processor's other limiters count REQUESTS (slot cap)
or RAM (memory budget). Neither can see the disk: one hundred 10 MB
saves and one hundred 900 MB saves are the same "100 slots" to them,
but 9 GB vs 90 GB of temp files. This module instead leases BYTES.

Before a request's download starts it must hold a lease for the total
size of the media it is about to pull down (the size is known from the
fetched Telegram messages — no byte has been downloaded yet, and the
parallel downloader preallocates the temp file, so a transfer's temp
footprint is exactly 1x its file size). The lease is released when the
request finishes, fails, or is cancelled.

Two classes (threshold: LINK_REQUEST_BIG_FILE_MB on the request's
TOTAL size):

  - small requests lease against almost the whole usable disk, so a
    flood of small files drains at full speed and finishes first;
  - big requests lease against a capped POOL (a percentage of usable),
    so a flood of huge videos can only ever hold that share of the
    disk — they keep moving, but slowly, and can never starve the
    small class out of the disk;
  - symmetrically, a GUARANTEE slice of usable is fenced off from
    small leases, so the big class always has room to start — it can
    never be starved by the small flood either.

Both protections are WORK-CONSERVING: they bind only while the class
they protect against is actually PRESENT. A class is present while it
holds leases, has leases waiting, or was granted a lease within the
idle-grace window (LINK_REQUEST_CLASS_IDLE_GRACE_SECONDS — the grace
covers the gaps between claim batches, so a momentary lull mid-flood
does not hand the whole disk over). The moment a class is absent, the
other class may borrow the ENTIRE usable disk: once the small flood
has drained, big transfers expand past the pool into the freed space
instead of crawling at the pool rate next to gigabytes of idle disk —
and while no big request is anywhere in sight, small leases are not
held back by the guarantee fence either.

The accepted trade-off of borrowing: existing leases are never
preempted, so if the big class borrowed the whole disk and a small
request then arrives, the small request waits for natural big
completions (leases release per file, and the first release usually
frees enough for one small file). The grace window keeps this rare:
a class that was active seconds ago still counts as present, so the
disk is only ever lent out after a genuine lull.

"Usable" is measured live at every admission decision:

    usable = max(0, temp_free_mb() - LINK_REQUEST_DISK_RESERVE_MB)

so the operator's reserve (default 3 GB) stays free no matter what.
Note this is deliberately conservative: temp_free_mb() already shrinks
as in-flight transfers materialise their preallocated files, and the
leased totals are checked against it on top of that — the reserve is
protected twice over rather than exactly accounted. Admission errs on
the side of waiting.

A single file larger than usable is not rejected outright: it may run
SOLO once nothing else is leased (and it fits the disk with a 1 GB
absolute floor to spare). A file that cannot fit even an otherwise
empty disk fails fast instead of waiting forever.

Fairness: when space frees, waiting SMALL leases are considered before
waiting BIG ones (small files finish first); a waiter that still does
not fit never blocks waiters of the other class behind it. The grant
rules themselves fence the big guarantee, so big waiters are never
starved by that priority.

LINK_REQUEST_DISK_RESERVE_MB = 0 disables the whole mechanism —
acquire() then grants instantly and tracks nothing, which is exactly
the pre-existing behaviour.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.logging import get_logger

log = get_logger(__name__)

#: A file may only be admitted (even solo, on an empty disk) if this
#: much space would STILL be free afterwards. Below this, basic
#: operation (DB writes, logs) is at risk, so the request fails fast.
_ABSOLUTE_FLOOR_MB = 1024.0

_EPS = 1e-9


class DiskLeaseTooLargeError(Exception):
    """The request's media cannot fit this disk at all — fail fast."""


@dataclass
class _Waiter:
    size_mb: float
    is_big: bool
    request_id: int | None
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False
    error: Exception | None = None


class DiskLease:
    """A granted byte lease. Release exactly once (idempotent)."""

    def __init__(
        self,
        manager: "DiskLeaseManager | None",
        size_mb: float,
        is_big: bool,
    ) -> None:
        self._manager = manager
        self.size_mb = size_mb
        self.is_big = is_big
        self._released = manager is None

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if self._manager is not None:
            self._manager._release_bytes(self.size_mb, self.is_big)

    async def __aenter__(self) -> "DiskLease":
        return self

    async def __aexit__(self, *exc) -> bool:
        self.release()
        return False


class DiskLeaseManager:
    """Asyncio-safe byte-lease admission over the temp filesystem.

    All state mutation happens in await-free code paths (grant passes
    are pure synchronous evaluation + event sets), so no lock is
    needed on a single event loop: a pass can never be interrupted
    halfway by another coroutine.
    """

    def __init__(
        self,
        *,
        reserve_mb: float,
        big_file_mb: float,
        big_pool_percent: float,
        big_guarantee_percent: float,
        free_mb_reader: Callable[[], float | None],
        idle_grace_seconds: float = 30.0,
        time_reader: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reserve_mb = float(reserve_mb)
        self._big_file_mb = float(big_file_mb)
        self._big_pool_percent = float(big_pool_percent)
        self._big_guarantee_percent = float(big_guarantee_percent)
        self._free_mb_reader = free_mb_reader
        self._idle_grace_seconds = float(idle_grace_seconds)
        self._time_reader = time_reader
        self._total_mb = 0.0
        self._big_mb = 0.0
        self._waiters: list[_Waiter] = []
        # Class presence (work-conserving borrowing): a class counts
        # as present while it holds leases, has waiters, or was last
        # granted a lease less than idle_grace_seconds ago. The grant
        # timestamps below feed the third condition.
        self._last_small_grant_at: float | None = None
        self._last_big_grant_at: float | None = None
        # Borrowing state derived from presence, tracked only so its
        # transitions can be logged: big borrows when small is absent,
        # small borrows when big is absent. None = not yet evaluated.
        self._big_borrowing: bool | None = None
        self._small_borrowing: bool | None = None

    # -- introspection -------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._reserve_mb > 0

    @property
    def reserve_mb(self) -> float:
        return self._reserve_mb

    @property
    def total_leased_mb(self) -> float:
        return self._total_mb

    @property
    def big_leased_mb(self) -> float:
        return self._big_mb

    @property
    def waiting_count(self) -> int:
        return len(self._waiters)

    def is_big(self, size_mb: float) -> bool:
        return size_mb > self._big_file_mb

    # -- admission ------------------------------------------------------

    async def acquire(
        self, size_mb: float, *, request_id: int | None = None
    ) -> DiskLease:
        """Wait until a lease of ``size_mb`` fits, then return it.

        Raises DiskLeaseTooLargeError when the size cannot fit this
        disk even with nothing else leased. Cancellation is safe: a
        cancelled waiter is dequeued, or its grant handed back.
        """
        size_mb = float(size_mb)
        if not self.enabled or size_mb <= 0:
            return DiskLease(None, 0.0, self.is_big(size_mb))
        waiter = _Waiter(
            size_mb=size_mb,
            is_big=self.is_big(size_mb),
            request_id=request_id,
        )
        self._waiters.append(waiter)
        self._grant_pass()
        if not waiter.granted and waiter.error is None:
            free = self._free_mb_reader()
            log.info(
                "link_request_disk_lease_waiting",
                request_id=request_id,
                size_mb=round(size_mb),
                is_big=waiter.is_big,
                total_leased_mb=round(self._total_mb),
                big_leased_mb=round(self._big_mb),
                usable_mb=(
                    round(max(0.0, free - self._reserve_mb))
                    if free is not None
                    else None
                ),
            )
        try:
            await waiter.event.wait()
        except asyncio.CancelledError:
            if waiter in self._waiters:
                self._waiters.remove(waiter)
            elif waiter.granted:
                self._release_bytes(waiter.size_mb, waiter.is_big)
            raise
        if waiter.error is not None:
            raise waiter.error
        return DiskLease(self, waiter.size_mb, waiter.is_big)

    def kick(self) -> None:
        """Re-run the grant pass (e.g. on the supervisor's tick).

        Covers two things releases cannot: waiters wake when EXTERNAL
        disk usage changes, and waiters unblock when a class's idle
        grace EXPIRES — the moment the other class may start borrowing
        the whole usable disk, no release needs to happen at all."""
        if self.enabled:
            self._grant_pass()

    # -- internals --------------------------------------------------------

    def _release_bytes(self, size_mb: float, is_big: bool) -> None:
        self._total_mb = max(0.0, self._total_mb - size_mb)
        if is_big:
            self._big_mb = max(0.0, self._big_mb - size_mb)
        self._grant_pass()

    def _class_present(self, *, is_big: bool, now: float) -> bool:
        """Is this class here right now — holding leases, waiting for
        one, or granted one within the idle-grace window? Only a
        PRESENT class's counterpart protections bind; see the module
        docstring (work-conserving borrowing)."""
        if is_big:
            if self._big_mb > _EPS:
                return True
            last_grant = self._last_big_grant_at
        else:
            if self._total_mb - self._big_mb > _EPS:
                return True
            last_grant = self._last_small_grant_at
        if any(w.is_big == is_big for w in self._waiters):
            return True
        return (
            last_grant is not None
            and (now - last_grant) < self._idle_grace_seconds
        )

    def _note_expansion(
        self, *, small_present: bool, big_present: bool
    ) -> None:
        """Track and log borrowing transitions: big borrows the whole
        usable disk while small is absent, small borrows (no guarantee
        fence) while big is absent. Evaluated on EVERY pass — acquire,
        release and kick() alike — so a grace expiry is noticed on the
        supervisor's next tick even when nothing else happened."""
        for cls, borrowing, attr in (
            ("big", not small_present, "_big_borrowing"),
            ("small", not big_present, "_small_borrowing"),
        ):
            previous = getattr(self, attr)
            if previous is not None and previous != borrowing:
                log.info(
                    "link_request_disk_lease_expansion_changed",
                    borrowing_class=cls,
                    borrowing=borrowing,
                    small_present=small_present,
                    big_present=big_present,
                )
            setattr(self, attr, borrowing)

    def _grant_pass(self) -> None:
        now = self._time_reader()
        small_present = self._class_present(is_big=False, now=now)
        big_present = self._class_present(is_big=True, now=now)
        self._note_expansion(
            small_present=small_present, big_present=big_present
        )
        if not self._waiters:
            return
        free = self._free_mb_reader()
        usable = None if free is None else max(0.0, free - self._reserve_mb)
        # Small waiters first (they finish first), then big waiters;
        # each is re-checked against the live totals, and one that
        # still does not fit never blocks the other class behind it.
        ordered = [w for w in self._waiters if not w.is_big] + [
            w for w in self._waiters if w.is_big
        ]
        for waiter in ordered:
            if waiter not in self._waiters:
                continue
            verdict = self._verdict(
                waiter.size_mb,
                waiter.is_big,
                free,
                usable,
                small_present=small_present,
                big_present=big_present,
            )
            if verdict == "grant":
                self._waiters.remove(waiter)
                self._total_mb += waiter.size_mb
                if waiter.is_big:
                    self._big_mb += waiter.size_mb
                    self._last_big_grant_at = now
                else:
                    self._last_small_grant_at = now
                waiter.granted = True
                waiter.event.set()
                log.info(
                    "link_request_disk_lease_granted",
                    request_id=waiter.request_id,
                    size_mb=round(waiter.size_mb),
                    is_big=waiter.is_big,
                    total_leased_mb=round(self._total_mb),
                    big_leased_mb=round(self._big_mb),
                    usable_mb=round(usable) if usable is not None else None,
                )
            elif verdict == "impossible":
                self._waiters.remove(waiter)
                waiter.error = DiskLeaseTooLargeError(
                    "That post is too large for the space left on the "
                    f"server's disk ({waiter.size_mb:.0f} MB needed, "
                    f"{free:.0f} MB free). Ask the operator to free disk "
                    "space or use a bigger server."
                    if free is not None
                    else "That post is too large for the server's disk."
                )
                waiter.event.set()
                log.warning(
                    "link_request_disk_lease_too_large",
                    request_id=waiter.request_id,
                    size_mb=round(waiter.size_mb),
                    free_mb=round(free) if free is not None else None,
                )

    def _verdict(
        self,
        size_mb: float,
        is_big: bool,
        free: float | None,
        usable: float | None,
        *,
        small_present: bool,
        big_present: bool,
    ) -> str:
        """'grant' | 'wait' | 'impossible' for one prospective lease."""
        if free is None or usable is None:
            # Disk size unknown (probe failed): never block the queue
            # on a guess — the same fallback the RAM/disk guards use.
            return "grant"
        if size_mb > usable:
            # Giant file (bigger than the whole usable budget): solo
            # admission only, on an otherwise idle disk, and only if
            # it fits with the absolute floor to spare. A size that
            # fails the floor is impossible once nothing is leased;
            # while leases are outstanding it waits, because their
            # completion frees real space. (Sizes WITHIN usable are
            # governed by the reserve through usable itself, so the
            # floor does not apply to them.)
            if size_mb > free - _ABSOLUTE_FLOOR_MB:
                return "impossible" if self._total_mb <= _EPS else "wait"
            return "grant" if self._total_mb <= _EPS else "wait"
        if self._total_mb + size_mb > usable:
            return "wait"
        if is_big:
            if small_present:
                # The pool caps the big class — with one exception: a
                # single big file bigger than the whole pool must
                # still be admittable on an empty disk, so the cap
                # never drops below the file's own size (bounded by
                # usable above).
                pool_cap = max(
                    usable * self._big_pool_percent / 100.0,
                    min(size_mb, usable),
                )
            else:
                # Small class absent: the pool is work-conserving and
                # big leases may borrow the whole usable disk — only
                # the hard total cap (checked above) still binds, so
                # this cap is exactly what remains after small leases.
                pool_cap = usable - (self._total_mb - self._big_mb)
            if self._big_mb + size_mb > pool_cap:
                return "wait"
        elif big_present:
            # Small leases may not eat the part of the big guarantee
            # the big class is not already using — but only while the
            # big class is present. While it is absent the fence is
            # dropped entirely (work-conserving): the hard total cap
            # above is the only limit.
            guarantee = usable * self._big_guarantee_percent / 100.0
            small_ceiling = usable - max(0.0, guarantee - self._big_mb)
            if self._total_mb + size_mb > small_ceiling:
                return "wait"
        return "grant"
