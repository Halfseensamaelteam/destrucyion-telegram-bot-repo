"""
tests/unit/core/test_disk_lease.py
Unit tests for app.core.disk_lease — size-aware two-class byte leases.

The disk is a mutable box: tests move "free space" by assignment and
watch admission react. All sizes are MB floats, matching the manager.
"""

import asyncio

import pytest

from app.core.disk_lease import DiskLeaseManager, DiskLeaseTooLargeError


class FakeDisk:
    """free_mb_reader stand-in: free space the test controls live."""

    def __init__(self, free_mb):
        self.free_mb = free_mb

    def __call__(self):
        return self.free_mb


class FakeClock:
    """time_reader stand-in: monotonic time the test advances by hand,
    so class presence / the idle-grace window is deterministic. A
    frozen clock means the grace window never expires mid-test unless
    the test advances it on purpose."""

    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def make_manager(
    disk,
    *,
    reserve=100.0,
    big_file=100.0,
    pool=30.0,
    guarantee=15.0,
    grace=30.0,
    clock=None,
):
    # Defaults: usable = free - 100; big pool = 30% of usable;
    # big guarantee = 15% of usable fenced off from small leases.
    return DiskLeaseManager(
        reserve_mb=reserve,
        big_file_mb=big_file,
        big_pool_percent=pool,
        big_guarantee_percent=guarantee,
        idle_grace_seconds=grace,
        free_mb_reader=disk,
        time_reader=clock if clock is not None else FakeClock(),
    )


async def test_small_lease_grants_immediately_and_tracks_totals():
    disk = FakeDisk(1100.0)  # usable 1000
    mgr = make_manager(disk)
    lease = await mgr.acquire(40.0, request_id=1)
    assert lease.size_mb == 40.0
    assert lease.is_big is False
    assert mgr.total_leased_mb == 40.0
    assert mgr.big_leased_mb == 0.0
    lease.release()
    assert mgr.total_leased_mb == 0.0
    lease.release()  # idempotent
    assert mgr.total_leased_mb == 0.0


async def test_total_cap_enforced_and_release_wakes_waiter():
    disk = FakeDisk(1100.0)  # usable 1000
    mgr = make_manager(disk)
    first = await mgr.acquire(700.0)
    waiter = asyncio.create_task(mgr.acquire(400.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()  # 700 + 400 > 1000 usable
    assert mgr.waiting_count == 1
    first.release()
    second = await asyncio.wait_for(waiter, timeout=1.0)
    assert mgr.total_leased_mb == 400.0
    second.release()


async def test_big_pool_cap_enforced():
    """While the small class is PRESENT, big leases share the pool cap
    — the work-conserving expansion only applies once small is gone."""
    disk = FakeDisk(1100.0)  # usable 1000 -> pool cap = max(300, size)
    mgr = make_manager(disk)
    small = await mgr.acquire(50.0)  # a small lease holds: small present
    first = await mgr.acquire(250.0)  # big: pool usage 250/300
    assert first.is_big is True
    waiter = asyncio.create_task(mgr.acquire(250.0))
    await asyncio.sleep(0.05)
    # Total would fit (550 <= 1000) but the big pool (300) would not.
    assert not waiter.done()
    first.release()
    second = await asyncio.wait_for(waiter, timeout=1.0)
    second.release()
    small.release()


async def test_single_big_file_bigger_than_pool_still_admitted():
    """The pool cap never drops below one file's own size: a 600 MB
    file on a 1000 MB usable disk is admitted although the pool is
    nominally 300 — otherwise big files could never run at all."""
    disk = FakeDisk(1100.0)
    mgr = make_manager(disk)
    lease = await mgr.acquire(600.0)
    assert mgr.big_leased_mb == 600.0
    lease.release()


async def test_small_cannot_eat_big_guarantee_and_big_still_gets_in():
    """Contention case (BOTH classes present): the guarantee fence
    caps the small flood and the big class still leases into it."""
    disk = FakeDisk(1100.0)  # usable 1000, guarantee 150
    mgr = make_manager(disk)
    # The big class counts as present — it was granted a lease a
    # moment ago, inside the idle-grace window — so the fence is up
    # even though it currently holds nothing.
    primer = await mgr.acquire(150.0)
    primer.release()
    flood = await mgr.acquire(85.0)  # small class flooding begins
    for _ in range(9):
        await mgr.acquire(85.0)  # 850 total leased by small class
    # Small ceiling = 1000 - 150 guarantee = 850: the next small
    # request must WAIT even though 150 MB of usable looks "free".
    small_waiter = asyncio.create_task(mgr.acquire(85.0))
    await asyncio.sleep(0.05)
    assert not small_waiter.done()
    # But the guarantee is real: a big file leases INTO it right away,
    # jumping the waiting small request — big is never starved.
    big = await mgr.acquire(150.0)
    assert mgr.big_leased_mb == 150.0
    # Total is now exactly 1000: the small waiter still cannot fit.
    await asyncio.sleep(0.05)
    assert not small_waiter.done()
    big.release()
    flood.release()
    granted = await asyncio.wait_for(small_waiter, timeout=1.0)
    granted.release()


async def test_waiting_small_granted_before_waiting_big():
    """When space frees, small waiters are considered first."""
    disk = FakeDisk(450.0)  # usable 350, guarantee 52.5, pool 105
    mgr = make_manager(disk)
    # Fill the disk exactly: 100 small plus one 250 big lease (its
    # pool exception cap is its own size, 250) = 350 usable.
    holders = [await mgr.acquire(100.0), await mgr.acquire(250.0)]
    assert mgr.total_leased_mb == 350.0
    big_waiter = asyncio.create_task(mgr.acquire(300.0))   # queued FIRST
    small_waiter = asyncio.create_task(mgr.acquire(100.0))  # queued SECOND
    await asyncio.sleep(0.05)
    assert not big_waiter.done() and not small_waiter.done()
    for lease in holders:
        lease.release()
    # Despite queueing second, the small waiter wins the freed space
    # (100 of 350); the big one then no longer fits the total
    # (100 + 300 > 350) and keeps waiting. Had the big waiter been
    # served first, the outcome would be exactly reversed.
    small = await asyncio.wait_for(small_waiter, timeout=1.0)
    await asyncio.sleep(0.05)
    assert not big_waiter.done()
    small.release()
    big = await asyncio.wait_for(big_waiter, timeout=1.0)
    big.release()


async def test_giant_file_runs_solo_only():
    # reserve 3072 like production: usable = 5000 - 3072 = 1928.
    disk = FakeDisk(5000.0)
    mgr = make_manager(disk, reserve=3072.0)
    small = await mgr.acquire(50.0)
    giant = asyncio.create_task(mgr.acquire(2500.0))  # > usable 1928
    await asyncio.sleep(0.05)
    assert not giant.done()  # must wait for an otherwise-empty disk
    small.release()
    lease = await asyncio.wait_for(giant, timeout=1.0)  # solo admission
    assert mgr.total_leased_mb == 2500.0
    lease.release()


async def test_too_large_fails_fast():
    disk = FakeDisk(5000.0)  # absolute floor: size must be <= 5000-1024
    mgr = make_manager(disk, reserve=3072.0)
    with pytest.raises(DiskLeaseTooLargeError):
        await asyncio.wait_for(mgr.acquire(4500.0), timeout=1.0)
    assert mgr.waiting_count == 0
    assert mgr.total_leased_mb == 0.0


async def test_too_large_waits_while_leases_outstanding_then_fails():
    """Impossible is only declared once the disk is otherwise empty:
    while leases are outstanding their completion frees real space."""
    disk = FakeDisk(5000.0)
    mgr = make_manager(disk, reserve=3072.0)
    holder = await mgr.acquire(60.0)
    giant = asyncio.create_task(mgr.acquire(4500.0))
    await asyncio.sleep(0.05)
    assert not giant.done()  # still hoping space frees up
    holder.release()  # now the disk is empty and it STILL cannot fit
    with pytest.raises(DiskLeaseTooLargeError):
        await asyncio.wait_for(giant, timeout=1.0)


async def test_reserve_zero_disables_admission():
    disk = FakeDisk(10.0)  # nearly full disk
    mgr = make_manager(disk, reserve=0.0)
    assert mgr.enabled is False
    lease = await mgr.acquire(99999.0)  # absurd size: granted instantly
    assert mgr.total_leased_mb == 0.0  # nothing is tracked
    lease.release()  # harmless no-op


async def test_unknown_free_space_never_blocks():
    disk = FakeDisk(None)  # probe failure -> reader returns None
    mgr = make_manager(disk)
    lease = await mgr.acquire(500.0)
    assert mgr.total_leased_mb == 500.0
    lease.release()


async def test_cancelled_waiter_is_dequeued():
    disk = FakeDisk(1100.0)
    mgr = make_manager(disk)
    holder = await mgr.acquire(950.0)
    waiter = asyncio.create_task(mgr.acquire(200.0))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert mgr.waiting_count == 0
    holder.release()
    assert mgr.total_leased_mb == 0.0


async def test_classification_boundary():
    disk = FakeDisk(1100.0)
    mgr = make_manager(disk, big_file=100.0)
    assert mgr.is_big(100.0) is False  # strictly greater than threshold
    assert mgr.is_big(100.1) is True


# ---------------------------------------------------------------------
# Work-conserving borrowing: protections bind only while the class
# they protect against is PRESENT (active / waiting / within grace).
# ---------------------------------------------------------------------


async def test_big_expands_after_small_drains_and_grace_passes():
    """Wood's scenario in miniature: while small is active the pool
    binds; after the small class drains AND the grace window passes,
    a kick ALONE (no release at that moment) grants the big waiter
    beyond the pool."""
    clock = FakeClock()
    disk = FakeDisk(1100.0)  # usable 1000, pool 300
    mgr = make_manager(disk, clock=clock)
    small = await mgr.acquire(50.0)
    big1 = await mgr.acquire(250.0)  # pool usage 250/300
    waiter = asyncio.create_task(mgr.acquire(400.0))
    await asyncio.sleep(0.05)
    # Total fits (700 <= 1000) but 250 + 400 > pool cap 400: blocked.
    assert not waiter.done()
    small.release()
    mgr.kick()
    await asyncio.sleep(0.05)
    # Small holds nothing any more, but its last grant is still
    # inside the grace window — expansion must NOT happen yet.
    assert not waiter.done()
    clock.advance(31.0)  # grace (30s) expires
    mgr.kick()  # no release happens here: the kick alone must grant
    lease = await asyncio.wait_for(waiter, timeout=1.0)
    assert mgr.big_leased_mb == 650.0  # 250 + 400, far past pool 300
    lease.release()
    big1.release()


async def test_big_does_not_expand_while_small_lease_active():
    """An ACTIVE small lease keeps the small class present forever —
    the grace window expiring changes nothing."""
    clock = FakeClock()
    disk = FakeDisk(1100.0)
    mgr = make_manager(disk, clock=clock)
    small = await mgr.acquire(50.0)
    big1 = await mgr.acquire(250.0)
    waiter = asyncio.create_task(mgr.acquire(400.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()
    clock.advance(3600.0)  # way past grace — irrelevant while active
    mgr.kick()
    await asyncio.sleep(0.05)
    assert not waiter.done()
    small.release()  # now small truly leaves (grant is ancient history)
    lease = await asyncio.wait_for(waiter, timeout=1.0)
    lease.release()
    big1.release()


async def test_big_blocked_by_grace_alone_then_expands():
    """Presence can come from the grace window ALONE: the small lease
    is released before the big class even arrives, yet the pool still
    binds until the window passes."""
    clock = FakeClock()
    disk = FakeDisk(1100.0)
    mgr = make_manager(disk, clock=clock)
    primer = await mgr.acquire(50.0)  # small was here...
    primer.release()  # ...but holds nothing from now on
    big1 = await mgr.acquire(250.0)  # granted within the pool (300)
    waiter = asyncio.create_task(mgr.acquire(400.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()  # pool binds thanks to grace alone
    clock.advance(29.0)  # still inside the 30s window
    mgr.kick()
    await asyncio.sleep(0.05)
    assert not waiter.done()
    clock.advance(2.0)  # 31s after the last small grant: window over
    mgr.kick()
    lease = await asyncio.wait_for(waiter, timeout=1.0)
    lease.release()
    big1.release()


async def test_small_fence_drops_when_big_class_absent():
    """No big request anywhere: small leases use the WHOLE usable
    disk — the 15% guarantee (150 MB here) is not fenced off."""
    disk = FakeDisk(1100.0)  # usable 1000; old fence ceiling was 850
    mgr = make_manager(disk)
    leases = [await mgr.acquire(100.0) for _ in range(10)]
    assert mgr.total_leased_mb == 1000.0  # leases 9 and 10 pass 850
    for lease in leases:
        lease.release()


async def test_small_fence_active_while_big_present():
    """Mirror image: while a big lease is active the guarantee fence
    binds small leases — and it binds by the part of the guarantee
    the big class is NOT using (150 - 120 = 30 fenced here)."""
    clock = FakeClock()
    disk = FakeDisk(1100.0)  # usable 1000, guarantee 150
    mgr = make_manager(disk, clock=clock)
    big = await mgr.acquire(120.0)  # big present, holding 120 of it
    leases = [await mgr.acquire(100.0) for _ in range(8)]  # small 800
    leases.append(await mgr.acquire(50.0))  # total hits ceiling 970
    assert mgr.total_leased_mb == 970.0
    waiter = asyncio.create_task(mgr.acquire(10.0))
    await asyncio.sleep(0.05)
    assert not waiter.done()  # 970 + 10 > usable - (150 - 120)
    big.release()
    await asyncio.sleep(0.05)
    # Counter-intuitive but correct: releasing the big lease does NOT
    # free small space here — the fence grows back to the full 150
    # (150 - 0), so the small ceiling (850) matches the leased 850.
    assert not waiter.done()
    clock.advance(31.0)  # big class leaves (grace over): fence drops
    mgr.kick()
    granted = await asyncio.wait_for(waiter, timeout=1.0)
    granted.release()
    for lease in leases:
        lease.release()
