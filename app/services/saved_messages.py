"""
app.services.saved_messages
~~~~~~~~~~~~~~~~~~~~~~~~~~~~
SavedMessagesService — forwards captured media to the account owner's
own Saved Messages within the *same* Telegram account.

Critical routing invariant (ROADMAP.md Phase 9 / CLAUDE.md §9):
  Account A → Account A's Saved Messages
  Account B → Account B's Saved Messages
  NEVER Account A → Account B's Saved Messages

Design:
  - The service receives an explicit (client, account_id) pair.
  - It resolves "me" from the client — never from configuration or a DB field.
  - If the client is not for account_id, it raises RoutingError immediately.
  - All forwarding failures are caught, logged, and propagated so that
    the caller (MediaCaptureService) can mark the record FAILED.
  - We forward using client.forward_messages(to=InputPeerSelf, ...) first
    (zero re-upload cost). If the message is no longer available (timed
    media already expired), we try send_file with the original file_id.
  - Self-destructing media is best-effort only (CLAUDE.md §8).
    Success/failure is always reported honestly.
"""

from __future__ import annotations

import asyncio
import functools
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.telegram.metadata import ChatInfo, SenderInfo, build_caption

log = get_logger(__name__)


class RoutingError(Exception):
    """Raised when the wrong client would be used for an account's Saved Messages."""


class SavedMessagesForwardError(Exception):
    """Raised when forwarding to Saved Messages fails."""


@dataclass
class ForwardResult:
    """Result of a Saved Messages forward attempt.

    Attributes:
        saved_message_id: The message ID of the forwarded message in Saved Messages.
        caption_used: The caption attached to the forwarded message.
        was_resent: True if we had to re-send (file-id path) instead of forward.
        file_path: Path to the downloaded temp file (if media was downloaded/re-sent).
    """

    saved_message_id: int
    caption_used: str
    was_resent: bool
    file_path: str | None = None


def with_flood_wait_retry(max_retries: int = 3):
    """Decorator to retry Telethon functions if they hit a FloodWaitError."""
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            from telethon.errors import FloodWaitError
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except FloodWaitError as e:
                    if attempt == max_retries:
                        raise
                    log.warning(
                        "flood_wait_encountered",
                        seconds=e.seconds,
                        attempt=attempt + 1,
                        msg="Sleeping before retry...",
                    )
                    await asyncio.sleep(e.seconds)
        return wrapper
    return decorator


_DOWNLOAD_PART_SIZE = 512 * 1024  # Telethon MAX_CHUNK_SIZE for GetFile


async def download_media_parallel(
    client,
    source_message,
    dest_dir: str,
    *,
    workers: int,
    progress_callback=None,
) -> str:
    """Download a document with ``workers`` parallel connections.

    Telethon's ``download_media`` pulls a file through a single
    connection; on a fast link that one stream is the bottleneck
    (~1.5 MB/s measured on a VPS for a 10 MB video). Telegram serves
    byte ranges, so the file is split into 512 KiB parts and ``workers``
    ``iter_download`` iterators fetch interleaved parts concurrently
    (offset = worker * part, stride = workers * part) — each iterator
    holds its own connection to the file's DC. Parts land at their exact
    offsets in one preallocated file, so assembly is just verification
    of the final size.

    Only for documents with a known size. Raises on any failure —
    callers fall back to the plain sequential download.
    """
    import os

    file_info = getattr(source_message, "file", None)
    total = getattr(file_info, "size", None)
    if not total:
        raise SavedMessagesForwardError("parallel download needs a known file size")

    name = getattr(file_info, "name", None) or f"document{getattr(file_info, 'ext', '') or '.bin'}"
    dest_path = os.path.join(dest_dir, name)
    part = _DOWNLOAD_PART_SIZE
    chunk_count = (total + part - 1) // part
    workers = max(1, min(workers, chunk_count))

    # Preallocate so every worker can write at its own offsets.
    with open(dest_path, "wb") as fh:
        fh.truncate(total)

    received = 0

    async def _worker(worker_index: int) -> None:
        nonlocal received
        offset = worker_index * part
        handle = open(dest_path, "r+b")
        try:
            position = offset
            async for chunk in client.iter_download(
                source_message,
                offset=offset,
                stride=workers * part,
                chunk_size=part,
                request_size=part,
            ):
                if not chunk:
                    break
                handle.seek(position)
                handle.write(chunk)
                position += workers * part
                received += len(chunk)
                if progress_callback is not None:
                    progress_callback(received, total)
        finally:
            handle.close()

    results = await asyncio.gather(
        *(_worker(i) for i in range(workers)), return_exceptions=True
    )
    for result in results:
        if isinstance(result, Exception):
            raise SavedMessagesForwardError(
                f"parallel download failed: {result}"
            ) from result

    if os.path.getsize(dest_path) != total or received != total:
        raise SavedMessagesForwardError(
            f"parallel download size mismatch: got {received}, expected {total}"
        )
    return dest_path


def _make_progress_logger(phase: str, account_id: int):
    """Build a Telethon progress_callback that logs at 25% steps.

    Telethon calls progress_callback(received_bytes, total_bytes) many
    times per transfer; logging every call would flood the log, so only
    the 0/25/50/75/100% crossings are emitted.
    """
    state = {"last_bucket": -1}

    def _callback(received: int, total: int) -> None:
        if not total:
            return
        bucket = int(received * 100 / total) // 25
        if bucket != state["last_bucket"]:
            state["last_bucket"] = bucket
            log.info(
                "saved_messages_transfer_progress",
                account_id=account_id,
                phase=phase,
                percent=int(received * 100 / total),
                received_bytes=received,
                total_bytes=total,
            )

    return _callback


class SavedMessagesService:
    """Forwards media from a captured event to the account's own Saved Messages.

    One instance per forward operation — it is NOT a singleton.

    Usage:
        svc = SavedMessagesService(client, account_id=managed.account_id)
        result = await svc.forward(source_message, sender_info, chat_info, record)
    """

    def __init__(self, client, account_id: int) -> None:
        """
        Args:
            client: The authenticated TelegramClient for this account.
            account_id: The app-level TelegramAccount.id — used for routing
                        validation and logging. Never used to resolve "me".
        """
        self._client = client
        self._account_id = account_id

    async def forward(
        self,
        source_message,
        *,
        sender_info: SenderInfo,
        chat_info: ChatInfo,
        record,  # MediaRecord — for metadata only, never modified here
    ) -> ForwardResult:
        """Forward source_message to this account's Saved Messages.

        Routing invariant: "me" is resolved from the live client.
        This method never uses an account_id to look up a peer —
        that would create a cross-account routing risk.

        Strategy:
          1. Try forward_messages() — preserves original media, zero re-upload.
          2. If that fails (e.g. timed media expired, forward locked chat),
             try send_file() with the raw InputDocument/InputPhoto.
          3. If both fail, raise SavedMessagesForwardError.

        Returns:
            ForwardResult with the saved_message_id and optional file_path.

        Raises:
            SavedMessagesForwardError: When all forwarding strategies fail.
        """
        from datetime import datetime, timezone

        caption = build_caption(
            sender=sender_info,
            chat=chat_info,
            media_type=record.media_type.value,
            ttl_seconds=record.ttl_seconds,
            captured_at=datetime.now(timezone.utc),
        )

        # Timed / self-destructing media: NEVER forward and NEVER re-send by
        # file reference. Telegram refuses to forward it, and Telethon's
        # reference re-send (utils.get_input_media) copies ttl_seconds onto
        # the new message, so the "saved" copy would self-destruct too (or be
        # rejected outright in Saved Messages). The only reliable way — and
        # what the original Saveit.py does — is to download the bytes and
        # upload them as a brand-new, non-timed file.
        #
        # Protected media (chat has "Restrict saving content" on, exposed
        # as Message.noforwards) is in the same class: Telegram refuses to
        # forward it at all (ChatForwardsRestrictedError), for Bot API and
        # MTProto alike — but a member account can still download the bytes.
        # Going straight to download+upload also saves one doomed RPC (and
        # its FloodWait risk) per protected post.
        is_protected = getattr(source_message, "noforwards", False) is True
        if record.ttl_seconds is not None or is_protected:
            try:
                result = await self._try_resend(source_message, caption)
                log.info(
                    "saved_messages_download_uploaded",
                    account_id=self._account_id,
                    record_id=record.id,
                    saved_msg_id=result.saved_message_id,
                    strategy="download_upload_timed",
                )
                return result
            except Exception as exc:
                log.error(
                    "saved_messages_timed_failed",
                    account_id=self._account_id,
                    record_id=record.id,
                    error=str(exc),
                )
                raise SavedMessagesForwardError(
                    f"Could not download and re-upload timed/protected media: {exc}"
                ) from exc

        # Strategy 1: forward (zero re-upload) — normal, non-timed media only
        try:
            result = await self._try_forward(source_message, caption)
            log.info(
                "saved_messages_forwarded",
                account_id=self._account_id,
                record_id=record.id,
                saved_msg_id=result.saved_message_id,
                strategy="forward",
            )
            return result
        except Exception as fwd_exc:
            log.warning(
                "saved_messages_forward_failed_trying_resend",
                account_id=self._account_id,
                record_id=record.id,
                error=str(fwd_exc),
            )

        # Strategy 2: resend via file reference/download
        try:
            result = await self._try_resend(source_message, caption)
            log.info(
                "saved_messages_resent",
                account_id=self._account_id,
                record_id=record.id,
                saved_msg_id=result.saved_message_id,
                strategy="resend",
            )
            return result
        except Exception as resend_exc:
            log.error(
                "saved_messages_all_strategies_failed",
                account_id=self._account_id,
                record_id=record.id,
                error=str(resend_exc),
            )
            raise SavedMessagesForwardError(
                f"All forwarding strategies failed: {resend_exc}"
            ) from resend_exc

    @with_flood_wait_retry(max_retries=3)
    async def _try_forward(self, source_message, caption: str) -> ForwardResult:
        """Attempt to forward the message to Saved Messages and pin the caption."""
        from telethon.tl.types import InputPeerSelf

        # Forward the original message (preserves media, no re-upload)
        forwarded = await self._client.forward_messages(
            entity=InputPeerSelf(),
            messages=source_message,
            from_peer=source_message.peer_id,
        )

        # forwarded may be a list; normalise
        fwd_msg = forwarded[0] if isinstance(forwarded, list) else forwarded
        saved_id = fwd_msg.id

        # Send caption as a follow-up text reply pinned to the forwarded message
        await self._client.send_message(
            entity=InputPeerSelf(),
            message=caption,
            reply_to=saved_id,
        )

        return ForwardResult(
            saved_message_id=saved_id,
            caption_used=caption,
            was_resent=False,
            file_path=None,
        )

    async def _download(
        self, source_message, tmp_dir: str, total_size, progress_callback
    ) -> str | None:
        """Download the source media into ``tmp_dir``.

        Uses the parallel multi-connection downloader for documents at
        or above the configured size threshold, and Telethon's plain
        sequential ``download_media`` otherwise — and as an automatic
        fallback when the parallel attempt fails for any reason, so a
        parallel-download problem can never make a save fail that the
        old path would have completed.
        """
        from app.core.config import get_settings

        settings = get_settings()
        workers = max(1, settings.download_workers)
        if (
            workers > 1
            and isinstance(total_size, int)
            and total_size >= settings.download_parallel_min_bytes
            and getattr(source_message, "document", None) is not None
        ):
            try:
                path = await download_media_parallel(
                    self._client,
                    source_message,
                    tmp_dir,
                    workers=workers,
                    progress_callback=progress_callback,
                )
                log.info(
                    "saved_messages_parallel_download_used",
                    account_id=self._account_id,
                    workers=workers,
                    size_bytes=total_size,
                )
                return path
            except Exception as exc:
                log.warning(
                    "saved_messages_parallel_download_failed_falling_back",
                    account_id=self._account_id,
                    error=str(exc),
                )
                # Remove any partial file so the sequential path starts
                # clean and tmp cleanup stays accurate.
                try:
                    import glob

                    for partial in glob.glob(os.path.join(tmp_dir, "*")):
                        os.remove(partial)
                except OSError:
                    pass
        return await self._client.download_media(
            source_message,
            file=tmp_dir,
            progress_callback=progress_callback,
        )

    @with_flood_wait_retry(max_retries=3)
    async def _try_resend(self, source_message, caption: str) -> ForwardResult:
        """Download the media, then upload it as a brand-new file.

        Same approach as the original Saveit.py (download_media -> send_file).
        The re-uploaded file carries NO ttl_seconds, so it is a permanent
        copy in Saved Messages. The file path is returned in ForwardResult so
        other handlers (e.g., admin_notify) can use it before cleanup.
        """
        from telethon.tl.types import InputPeerSelf, DocumentAttributeVideo

        media = getattr(source_message, "media", None)
        if media is None:
            raise SavedMessagesForwardError("Source message has no media to resend")

        tmp_dir = tempfile.mkdtemp(prefix="destrucyion_")
        try:
            # Progress logging: a download+upload of a large video can take
            # minutes, and a silent worker looks exactly like a hung one
            # (the reference bots show a live % for this reason). Log at
            # 0/25/50/75/100% so the worker log proves the transfer is alive.
            total_size = getattr(getattr(source_message, "file", None), "size", None)
            log.info(
                "saved_messages_resend_starting",
                account_id=self._account_id,
                size_bytes=total_size,
            )
            progress_cb = _make_progress_logger("download", self._account_id)
            file_path = await self._download(
                source_message, tmp_dir, total_size, progress_cb
            )
            if not file_path:
                raise SavedMessagesForwardError(
                    "Telegram did not return a downloadable file"
                )
            log.info(
                "saved_messages_download_complete",
                account_id=self._account_id,
                file_path=file_path,
            )

            # Extract video metadata for proper video attributes
            video_attrs = None
            thumb_path = None
            
            # Check if this is a video and extract metadata
            document = getattr(source_message, "document", None)
            if document:
                for attr in document.attributes:
                    if isinstance(attr, DocumentAttributeVideo):
                        video_attrs = attr
                        break
            
            # Download thumbnail if video
            if video_attrs:
                try:
                    thumb_path = await self._client.download_media(
                        source_message, thumb=-1, file=tmp_dir
                    )
                except Exception as e:
                    log.warning("thumbnail_download_failed", error=str(e))

            # Prepare send_file parameters
            send_file_kwargs = {
                "entity": InputPeerSelf(),
                "file": file_path,
                "caption": caption,
                "supports_streaming": True,
            }
            
            # Add video attributes if available
            if video_attrs:
                from telethon.tl.types import DocumentAttributeVideo
                attrs = [
                    DocumentAttributeVideo(
                        duration=int(video_attrs.duration) if video_attrs.duration else 0,
                        w=video_attrs.w if video_attrs.w else 0,
                        h=video_attrs.h if video_attrs.h else 0,
                        supports_streaming=True,
                    )
                ]
                send_file_kwargs["attributes"] = attrs
            
            # Add thumbnail if downloaded
            if thumb_path and os.path.exists(thumb_path):
                send_file_kwargs["thumb"] = thumb_path

            send_file_kwargs["progress_callback"] = _make_progress_logger(
                "upload", self._account_id
            )
            sent = await self._client.send_file(**send_file_kwargs)
            log.info(
                "saved_messages_upload_complete",
                account_id=self._account_id,
                saved_msg_id=getattr(sent, "id", None),
            )

            return ForwardResult(
                saved_message_id=sent.id,
                caption_used=caption,
                was_resent=True,
                file_path=str(file_path),
            )
        except Exception:
            # Jika gagal di tengah jalan, bersihkan direktori temp yang dibuat
            if 'file_path' in locals() and file_path and os.path.exists(file_path):
                try:
                    os.remove(file_path)
                except OSError:
                    pass
            if 'thumb_path' in locals() and thumb_path and os.path.exists(thumb_path):
                try:
                    os.remove(thumb_path)
                except OSError:
                    pass
            if os.path.exists(tmp_dir):
                try:
                    for item in os.listdir(tmp_dir):
                        item_path = os.path.join(tmp_dir, item)
                        if os.path.isfile(item_path):
                            os.remove(item_path)
                    os.rmdir(tmp_dir)
                except OSError:
                    pass
            raise