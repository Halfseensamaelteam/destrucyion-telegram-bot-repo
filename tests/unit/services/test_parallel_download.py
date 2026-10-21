"""Parallel multi-connection downloader tests.

The fake client's iter_download mirrors Telethon's semantics: starting
at ``offset``, each request returns up to ``request_size`` bytes, then
the offset advances by ``stride``; iteration ends when a request
returns fewer bytes than requested (or nothing).
"""

from __future__ import annotations

import os
import random
from types import SimpleNamespace

import pytest

from app.services.saved_messages import (
    SavedMessagesForwardError,
    download_media_parallel,
)

PART = 512 * 1024


class FakeClient:
    def __init__(self, payload: bytes, fail: bool = False):
        self._payload = payload
        self._fail = fail
        self.iter_calls = 0

    async def iter_download(self, message, *, offset=0, stride=None, chunk_size=None, request_size=None):
        self.iter_calls += 1
        if self._fail:
            raise RuntimeError("boom")
        position = offset
        while True:
            chunk = self._payload[position:position + request_size]
            if not chunk:
                return
            yield chunk
            if len(chunk) < request_size:
                return
            position += stride


def make_message(size: int, name: str = "video.mp4"):
    return SimpleNamespace(
        file=SimpleNamespace(size=size, name=name, ext=".mp4"),
        document=object(),
    )


@pytest.mark.asyncio
async def test_parallel_download_reassembles_exact_bytes(tmp_path):
    payload = random.Random(42).randbytes(PART * 5 + 12345)  # uneven tail
    client = FakeClient(payload)
    path = await download_media_parallel(
        client, make_message(len(payload)), str(tmp_path), workers=4
    )
    assert os.path.basename(path) == "video.mp4"
    with open(path, "rb") as fh:
        assert fh.read() == payload
    assert client.iter_calls == 4  # one iterator per worker


@pytest.mark.asyncio
async def test_parallel_download_single_chunk_file(tmp_path):
    payload = b"x" * 1000  # far below one part
    client = FakeClient(payload)
    path = await download_media_parallel(
        client, make_message(len(payload)), str(tmp_path), workers=4
    )
    with open(path, "rb") as fh:
        assert fh.read() == payload


@pytest.mark.asyncio
async def test_parallel_download_exact_multiple_of_part(tmp_path):
    payload = random.Random(7).randbytes(PART * 4)  # no partial tail
    client = FakeClient(payload)
    path = await download_media_parallel(
        client, make_message(len(payload)), str(tmp_path), workers=4
    )
    with open(path, "rb") as fh:
        assert fh.read() == payload


@pytest.mark.asyncio
async def test_parallel_download_reports_progress(tmp_path):
    payload = random.Random(3).randbytes(PART * 3)
    seen = []
    await download_media_parallel(
        FakeClient(payload),
        make_message(len(payload)),
        str(tmp_path),
        workers=2,
        progress_callback=lambda received, total: seen.append((received, total)),
    )
    assert seen, "progress callback never fired"
    assert seen[-1] == (len(payload), len(payload))


@pytest.mark.asyncio
async def test_parallel_download_raises_on_client_failure(tmp_path):
    payload = b"y" * (PART * 2)
    with pytest.raises(SavedMessagesForwardError):
        await download_media_parallel(
            FakeClient(payload, fail=True),
            make_message(len(payload)),
            str(tmp_path),
            workers=2,
        )


@pytest.mark.asyncio
async def test_parallel_download_requires_known_size(tmp_path):
    message = SimpleNamespace(file=SimpleNamespace(size=None, name="a.mp4", ext=".mp4"))
    with pytest.raises(SavedMessagesForwardError):
        await download_media_parallel(
            FakeClient(b""), message, str(tmp_path), workers=2
        )
