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


def make_manager(disk, *, reserve=100.0, big_file=100.0, pool=30.0, guarantee=15.0):
    # Defaults: usable = free - 100; big pool = 30% of usable;
    # big guarantee = 15% of usable fenced off from small leases.
    return DiskLeaseManager(
        reserve_mb=reserve,
        big_file_mb=big_file,
        big_pool_percent=pool,
        big_guarantee_percent=guarantee,
        free_mb_reader=disk,
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
    disk = FakeDisk(1100.0)  # usable 1000 -> pool cap = max(300, size)
    mgr = make_manager(disk)
    first = await mgr.acquire(250.0)  # big: pool usage 250/300
    assert first.is_big is True
    waiter = asyncio.create_task(mgr.acquire(250.0))
    await asyncio.sleep(0.05)
    # Total would fit (500 <= 1000) but the big pool (300) would not.
    assert not waiter.done()
    first.release()
    second = await asyncio.wait_for(waiter, timeout=1.0)
    second.release()


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
    disk = FakeDisk(1100.0)  # usable 1000, guarantee 150
    mgr = make_manager(disk)
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
