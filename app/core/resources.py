"""app/core/resources.py — tiny process-resource probes (stdlib only).

Used by the link-request concurrency controller to size its claim
batches against a memory budget: the worker must never let transfer
parallelism eat RAM the box does not have.
"""

from __future__ import annotations

import os


def process_rss_mb() -> float | None:
    """Current resident set size of this process in MiB, or None.

    Linux: /proc/self/statm (the worker always runs in a Linux
    container). Returns None where /proc is unavailable — callers must
    treat None as "unknown" and fall back to fixed limits.
    """
    try:
        with open("/proc/self/statm") as fh:
            resident_pages = int(fh.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE") / (1024 * 1024)
    except Exception:
        return None


def temp_free_mb() -> float | None:
    """Free space (MiB) on the filesystem holding the temp dir, or None.

    Transfer temp files live in tempfile.gettempdir(); their aggregate
    size scales with in-flight transfers, so the concurrency controller
    consults this before opening more slots.
    """
    try:
        import shutil
        import tempfile

        return shutil.disk_usage(tempfile.gettempdir()).free / (1024 * 1024)
    except Exception:
        return None
