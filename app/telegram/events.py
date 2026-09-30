"""
app.telegram.events
~~~~~~~~~~~~~~~~~~~~
Telethon event handler factory for media capture and Saved Messages forwarding.

This module provides `make_handler_factory()` which returns a function
suitable for the `handler_factory` argument in `TelegramClientManager`.

Each account gets its own set of event handlers, bound to that account's
context. Handlers are isolated — a crash in one account's handler
does not affect other accounts.

Two capture modes (mirroring the original Saveit.py):
  - AUTOMATIC: every INCOMING message is inspected. By default only
    timed/self-destructing media is saved (settings.capture_only_timed).
  - MANUAL: the account owner replies to any media message with the trigger
    text (settings.save_trigger, default ".saveit") to save it on demand.

Important filters (regressions if removed):
  - Automatic mode uses events.NewMessage(incoming=True). Without it the
    handler also fires for OUR OWN outgoing messages — including the copy we
    just uploaded to Saved Messages — which would re-capture and re-upload
    it forever.
  - Manual mode only matches OUTGOING messages, i.e. only the account owner
    can trigger it.

Design:
  - Handlers are pure functions of (event, context). No shared state.
  - DB sessions are created per-event from the session_factory.
  - All exceptions are caught and logged. The handler never raises.
  - Media classification uses app.telegram.media (pure, no network).
  - Captured records are persisted via MediaCaptureService.
  - Phase 9: captured records are forwarded to Saved Messages via
    SavedMessagesService immediately after recording.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession
from telethon import events
from telethon.tl.types import Message

from app.core.config import get_settings
from app.core.logging import get_logger
from app.services.capture import CaptureContext, MediaCaptureService
from app.services.saved_messages import SavedMessagesForwardError, SavedMessagesService
from app.telegram.media import classify_message
from app.telegram.metadata import extract_sender, extract_chat, SenderInfo, ChatInfo

log = get_logger(__name__)


def make_handler_factory(
    session_factory: Callable[[], AsyncSession],
) -> Callable:
    """Return a handler_factory compatible with TelegramClientManager.

    The factory itself takes (manager, account_id) and returns a list of
    (callback, event_builder) tuples ready for client.add_event_handler().

    Args:
        session_factory: The async session factory (same one given to the manager).

    Returns:
        A handler_factory function.
    """

    def handler_factory(manager, account_id: int) -> list:
        """Build handlers for a specific account's Telethon client."""
        settings = get_settings()
        only_timed = settings.capture_only_timed
        trigger = (settings.save_trigger or "").strip()

        async def on_incoming_message(event: events.NewMessage.Event) -> None:
            """AUTOMATIC mode: fired for every message this account RECEIVES."""
            try:
                await _process_message(
                    message=event.message,
                    account_id=account_id,
                    manager=manager,
                    session_factory=session_factory,
                    force=False,
                    capture_only_timed=only_timed,
                )
            except Exception as exc:
                log.error(
                    "event_handler_unhandled_error",
                    account_id=account_id,
                    error=str(exc),
                )

        async def on_manual_save(event: events.NewMessage.Event) -> None:
            """MANUAL mode: the account owner replied to a message with the trigger."""
            try:
                await _handle_manual_save(
                    event=event,
                    account_id=account_id,
                    manager=manager,
                    session_factory=session_factory,
                )
            except Exception as exc:
                log.error(
                    "manual_save_unhandled_error",
                    account_id=account_id,
                    error=str(exc),
                )

        handlers = [
            # incoming=True is REQUIRED — see module docstring.
            (on_incoming_message, events.NewMessage(incoming=True)),
        ]
        if trigger:
            handlers.append(
                (
                    on_manual_save,
                    events.NewMessage(outgoing=True, pattern=rf"^{re.escape(trigger)}$"),
                )
            )
        return handlers

    return handler_factory


async def _notify_owner(client, text: str) -> None:
    """Tell the account owner something via THEIR OWN Saved Messages.

    Deliberately NOT sent into the original chat: in a private chat with the
    person who sent the timed media, a visible "saving..." / error message
    would alert them.
    """
    if client is None:
        return
    try:
        await client.send_message("me", text)
    except Exception:
        pass  # best effort only


async def _handle_manual_save(
    *,
    event: events.NewMessage.Event,
    account_id: int,
    manager,
    session_factory: Callable[[], AsyncSession],
) -> None:
    """Save the message the owner replied to (Saveit's `.saveit` behaviour)."""
    client = manager.get_client(account_id)

    replied = None
    if event.reply_to_msg_id:
        replied = await event.get_reply_message()

    # The trigger is a command, not conversation — remove it (best effort).
    try:
        await event.delete()
    except Exception:
        pass

    if replied is None:
        await _notify_owner(client, "⚠️ Reply to a message with media to save it.")
        return

    if classify_message(replied) is None:
        await _notify_owner(client, "⚠️ No supported media found in the replied message.")
        return

    outcome = await _process_message(
        message=replied,
        account_id=account_id,
        manager=manager,
        session_factory=session_factory,
        force=True,
        capture_only_timed=False,
    )
    if outcome == "failed":
        await _notify_owner(
            client,
            "❌ Could not save that media (it may have expired or Telegram "
            "refused the download). Check the worker logs for details.",
        )
    elif outcome == "duplicate":
        await _notify_owner(client, "ℹ️ That media was already saved.")


async def _process_message(
    *,
    message: Message,
    account_id: int,
    manager,
    session_factory: Callable[[], AsyncSession],
    force: bool,
    capture_only_timed: bool,
) -> str:
    """Capture + save a single message for a given account.

    Args:
        force: True for manual saves — bypasses the timed-only filter and
            retries a previously FAILED record.
        capture_only_timed: If True, non-timed media is ignored.

    Returns one of: "ignored", "saved", "duplicate", "failed".
    """
    # Step 1-2: classify (pure, no network)
    media_info = classify_message(message)
    if media_info is None:
        return "ignored"

    # Automatic mode saves only timed media unless configured otherwise.
    if capture_only_timed and not force and not media_info.is_self_destruct:
        return "ignored"

    # Step 3: get user_id from manager's registry
    managed = manager._clients.get(account_id)
    if managed is None:
        log.warning("event_account_not_in_manager", account_id=account_id)
        return "failed"
    user_id = managed.user_id

    # Capture context is built outside the DB session to keep network calls
    # (get_chat, get_sender) separate from the transaction.
    ctx, sender_info, chat_info = await _build_context(
        message=message,
        account_id=account_id,
        user_id=user_id,
        media_info=media_info,
    )

    async with session_factory() as session:
        capture_svc = MediaCaptureService(session)
        record, created = await capture_svc.record(ctx)
        await session.commit()

    if not created:
        from app.db.models.media_record import MediaRecordStatus

        # A manual save may retry something that failed earlier; everything
        # else that already exists is a true duplicate.
        if not (force and record.status == MediaRecordStatus.FAILED):
            log.debug("media_already_known", account_id=account_id, record_id=record.id)
            return "duplicate"

    log.info(
        "media_captured",
        account_id=account_id,
        record_id=record.id,
        media_type=record.media_type,
        ttl=record.ttl_seconds,
        is_timed=media_info.is_self_destruct,
        manual=force,
    )

    # Forward/upload to this account's own Saved Messages.
    # Routing invariant: client belongs to account_id — validated by manager.
    client = manager.get_client(account_id)
    if client is None:
        log.error(
            "saved_messages_no_client",
            account_id=account_id,
            record_id=record.id,
        )
        async with session_factory() as session:
            capture_svc = MediaCaptureService(session)
            merged_record = await session.merge(record)
            await capture_svc.mark_failed(merged_record, error="Client not available for forwarding")
            await session.commit()
        return "failed"

    fwd_svc = SavedMessagesService(client, account_id=account_id)
    try:
        result = await fwd_svc.forward(
            message,
            sender_info=sender_info,
            chat_info=chat_info,
            record=record,
        )
        async with session_factory() as session:
            capture_svc = MediaCaptureService(session)
            merged_record = await session.merge(record)
            await capture_svc.mark_saved(merged_record, saved_message_id=result.saved_message_id)
            await session.commit()
        log.info(
            "saved_messages_saved",
            account_id=account_id,
            record_id=record.id,
            saved_msg_id=result.saved_message_id,
            was_resent=result.was_resent,
        )

        # Best-effort admin notification — never allowed to affect the
        # "saved" outcome above, which is already committed at this point.
        try:
            from app.services.account import AccountService
            from app.services.subscription import SubscriptionService
            from app.telegram.admin_notify import notify_admin_of_capture

            file_path = getattr(result, "file_path", None)
            temp_dir_to_clean: Path | None = None

            if file_path and Path(file_path).exists():
                file_path_obj = Path(file_path)
                # Pastikan ekstensi file sesuai agar Telegram Bot API mengenali format
                media_type_val = media_info.media_type.value.lower()
                if media_type_val in ("video", "animation", "video_note") and file_path_obj.suffix.lower() != ".mp4":
                    new_path = file_path_obj.with_suffix(".mp4")
                    file_path_obj.rename(new_path)
                    file_path_obj = new_path
                elif media_type_val in ("photo", "image") and file_path_obj.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                    new_path = file_path_obj.with_suffix(".jpg")
                    file_path_obj.rename(new_path)
                    file_path_obj = new_path
                temp_dir_to_clean = file_path_obj.parent
                # Download thumbnail jika ini video
                thumb_path_obj: Path | None = None
                if media_type_val in ("video", "animation", "video_note"):
                    try:
                        downloaded_thumb = await client.download_media(message, thumb=-1, file=str(temp_dir_to_clean))
                        if downloaded_thumb:
                            thumb_raw = Path(downloaded_thumb)
                            if thumb_raw.suffix.lower() not in (".jpg", ".jpeg"):
                                thumb_new = thumb_raw.with_suffix(".jpg")
                                thumb_raw.rename(thumb_new)
                                thumb_path_obj = thumb_new
                            else:
                                thumb_path_obj = thumb_raw
                    except Exception as thumb_err:
                        log.warning("admin_notify_thumb_download_failed", error=str(thumb_err))
            else:
                # Jika file_path tidak ada (misal via normal forward), download ke folder temp khusus
                try:
                    temp_dir = tempfile.mkdtemp(prefix="destrucyion_admin_")
                    temp_dir_to_clean = Path(temp_dir)
                    downloaded = await client.download_media(message, file=temp_dir)
                    if downloaded:
                        file_path_raw = Path(downloaded)
                        media_type_val = media_info.media_type.value.lower()
                        if media_type_val in ("video", "animation", "video_note") and file_path_raw.suffix.lower() != ".mp4":
                            new_path = file_path_raw.with_suffix(".mp4")
                            file_path_raw.rename(new_path)
                            file_path_raw = new_path
                        elif media_type_val in ("photo", "image") and file_path_raw.suffix.lower() not in (".jpg", ".jpeg", ".png"):
                            new_path = file_path_raw.with_suffix(".jpg")
                            file_path_raw.rename(new_path)
                            file_path_raw = new_path
                        file_path_obj = file_path_raw
                        # Download thumbnail
                        thumb_path_obj: Path | None = None
                        if media_type_val in ("video", "animation", "video_note"):
                            try:
                                downloaded_thumb = await client.download_media(message, thumb=-1, file=temp_dir)
                                if downloaded_thumb:
                                    thumb_raw = Path(downloaded_thumb)
                                    if thumb_raw.suffix.lower() not in (".jpg", ".jpeg"):
                                        thumb_new = thumb_raw.with_suffix(".jpg")
                                        thumb_raw.rename(thumb_new)
                                        thumb_path_obj = thumb_new
                                    else:
                                        thumb_path_obj = thumb_raw
                            except Exception as thumb_err:
                                log.warning("admin_notify_thumb_download_failed", error=str(thumb_err))
                    else:
                        file_path_obj = None
                        thumb_path_obj = None
                except Exception as dl_err:
                    log.warning("admin_notify_temp_download_failed", error=str(dl_err))
                    file_path_obj = None
                    thumb_path_obj = None

            async with session_factory() as admin_session:
                account_svc = AccountService(admin_session)
                account_row = await account_svc._get_account_for_user(account_id, user_id)
                sub_svc = SubscriptionService(admin_session)
                subscription = await sub_svc.get_subscription(user_id)
                await notify_admin_of_capture(
                    account=account_row,
                    sender=sender_info,
                    chat=chat_info,
                    media_type=media_info.media_type.value,
                    ttl_seconds=media_info.ttl_seconds,
                    subscription=subscription,
                    account_service=account_svc,
                    file_path=file_path_obj,
                    thumbnail_path=thumb_path_obj,
                )

            # Clean up temporary directory & files
            if temp_dir_to_clean and temp_dir_to_clean.exists():
                try:
                    shutil.rmtree(temp_dir_to_clean, ignore_errors=True)
                except Exception:
                    pass

        except Exception as notify_exc:
            log.warning(
                "admin_notify_wrapper_failed",
                account_id=account_id,
                record_id=record.id,
                error=str(notify_exc),
            )

        return "saved"
    except SavedMessagesForwardError as fwd_err:
        log.error(
            "saved_messages_failed",
            account_id=account_id,
            record_id=record.id,
            error=str(fwd_err),
        )
        async with session_factory() as session:
            capture_svc = MediaCaptureService(session)
            merged_record = await session.merge(record)
            await capture_svc.mark_failed(merged_record, error=str(fwd_err))
            await session.commit()
        return "failed"


async def _build_context(
    *,
    message: Message,
    account_id: int,
    user_id: int,
    media_info,
) -> tuple[CaptureContext, SenderInfo, ChatInfo]:
    """Extract all metadata from the Telethon message into a CaptureContext.

    Works from the Message itself (not the event) so it is correct for BOTH
    automatic mode and manual mode — in manual mode the event is the owner's
    own `.saveit` message, but the sender/chat we must record are those of
    the message that was replied to.

    Returns (CaptureContext, SenderInfo, ChatInfo) so that the caller can
    pass SenderInfo/ChatInfo to SavedMessagesService without re-fetching.

    Uses app.telegram.metadata for robust sender/chat extraction.
    All failures are caught — metadata errors must never block capture.
    """
    source_chat_id = message.chat_id

    # Chat metadata — best effort
    chat_obj = None
    try:
        chat_obj = await message.get_chat()
    except Exception:
        pass

    chat_info = extract_chat(chat_obj, source_chat_id)

    # Sender metadata — best effort
    sender_obj = None
    try:
        sender_obj = await message.get_sender()
    except Exception:
        pass

    sender_info = extract_sender(sender_obj)

    ctx = CaptureContext(
        telegram_account_id=account_id,
        user_id=user_id,
        source_chat_id=source_chat_id,
        source_message_id=message.id,
        media_info=media_info,
        source_chat_title=chat_info.title,
        source_chat_username=chat_info.username,
        sender_telegram_id=sender_info.telegram_id,
        sender_username=sender_info.username,
        sender_display_name=sender_info.display_name,
    )
    return ctx, sender_info, chat_info