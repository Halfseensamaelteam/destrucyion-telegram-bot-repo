"""
app.telegram.events
~~~~~~~~~~~~~~~~~~~~
Telethon event handler factory for media capture and Saved Messages forwarding.

This module provides `make_handler_factory()` which returns a function
suitable for the `handler_factory` argument in `TelegramClientManager`.

Each account gets its own set of event handlers, bound to that account's
context. Handlers are isolated — a crash in one account's handler
does not affect other accounts.

Three capture modes (mirroring the original Saveit.py, plus link fetch):
  - AUTOMATIC: every INCOMING message is inspected. By default only
    timed/self-destructing media is saved (settings.capture_only_timed) —
    plus, when settings.capture_protected_content is on, media from chats
    with "Restrict saving content" enabled (noforwards), which likewise
    can never be retrieved by forwarding afterwards.
  - MANUAL: the account owner replies to any media message with the trigger
    text (settings.save_trigger, default ".saveit") to save it on demand.
  - LINK: the account owner sends "<restricted_trigger> <t.me link>"
    (default ".get ...") in their own Saved Messages. The worker fetches
    that post — private/restricted channels included, invite links join
    first — on this account's own client and saves a copy here. Bot-chat
    submissions (/save) take the same path via LinkRequest rows drained
    by app.services.restricted.LinkRequestProcessor.

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
        restricted_trigger = (settings.restricted_trigger or "").strip()

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
                    capture_protected_content=settings.capture_protected_content,
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

        async def on_restricted_link(event: events.NewMessage.Event) -> None:
            """LINK mode: owner sends '<restricted_trigger> <t.me link>'.

            Restricted to the owner's own Saved Messages chat — the same
            place saved copies land — so the command can never leak into
            (or be triggered from) a conversation with someone else.
            """
            try:
                await _handle_restricted_link(
                    event=event,
                    account_id=account_id,
                    manager=manager,
                    session_factory=session_factory,
                )
            except Exception as exc:
                log.error(
                    "restricted_link_unhandled_error",
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
        if restricted_trigger:
            handlers.append(
                (
                    on_restricted_link,
                    events.NewMessage(
                        outgoing=True,
                        pattern=rf"(?i)^{re.escape(restricted_trigger)}\s+\S+",
                    ),
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


async def _handle_restricted_link(
    *,
    event: events.NewMessage.Event,
    account_id: int,
    manager,
    session_factory: Callable[[], AsyncSession],
) -> None:
    """Fetch a post by t.me link (private/restricted included) for the owner.

    The owner sends ``.get https://t.me/c/<id>/<msg>`` (or a range, or an
    invite link) in their own Saved Messages. Everything runs on THIS
    account's client — the account must already be a member of a private
    chat; invite links join first. Copies land back in Saved Messages via
    the normal _process_message pipeline (DB idempotency included), so a
    post that was already captured is reported as a duplicate, not re-saved.
    """
    import asyncio

    from app.services.restricted import (
        RestrictedContentError,
        RestrictedContentService,
        copy_text_to_saved,
        join_by_invite,
    )
    from app.telegram.link_parser import InviteRef, parse_link

    settings = get_settings()
    client = manager.get_client(account_id)
    if client is None:
        return

    # Only honour the command inside the owner's Saved Messages chat.
    # event.chat_id for Saved Messages is the owner's own user id.
    try:
        me = await client.get_me()
        if me is None or event.chat_id != me.id:
            return
    except Exception:
        return

    raw_text = (event.raw_text or "").strip()
    # The trigger is a command, not conversation — remove it (best effort),
    # the same way .saveit removes itself.
    try:
        await event.delete()
    except Exception:
        pass

    # Everything after the trigger word is the link (link may be the whole rest).
    link_text = raw_text.split(maxsplit=1)[1] if len(raw_text.split(maxsplit=1)) > 1 else ""
    parsed = parse_link(link_text)
    if parsed is None:
        await _notify_owner(
            client,
            "⚠️ That does not look like a Telegram post/invite link.\n"
            "Examples:\n"
            "• .get https://t.me/channel/123\n"
            "• .get https://t.me/c/1234567/100-110\n"
            "• .get https://t.me/+AbCdEfGh (join first, then send the post link)",
        )
        return

    if isinstance(parsed, InviteRef):
        try:
            result = await join_by_invite(client, parsed.invite_hash)
            await _notify_owner(
                client,
                f"✅ Invite handled ({result}). Now send the post link with "
                f"{settings.restricted_trigger} and it will be saved here.",
            )
        except RestrictedContentError as exc:
            await _notify_owner(client, f"❌ {exc}")
        return

    service = RestrictedContentService(client, account_id=account_id)
    ids = parsed.message_ids[: settings.restricted_max_batch]
    if len(parsed.message_ids) > len(ids):
        await _notify_owner(
            client,
            f"⚠️ Range capped at {settings.restricted_max_batch} posts "
            f"(you asked for {len(parsed.message_ids)}).",
        )

    saved = duplicate = failed = 0
    for index, msg_id in enumerate(ids):
        from app.telegram.link_parser import PostRef

        single = PostRef(
            peer=parsed.peer,
            message_ids=(msg_id,),
            is_private=parsed.is_private,
            topic_id=parsed.topic_id,
        )
        try:
            (message,) = await service.fetch_messages(single)
        except RestrictedContentError as exc:
            await _notify_owner(client, f"❌ {exc}")
            return
        if message is None:
            failed += 1
            continue
        if classify_message(message) is None:
            # Text-only post: protected text cannot be forwarded either —
            # re-send it as a new Saved Messages note.
            text = getattr(message, "text", None) or getattr(message, "message", None)
            if text:
                try:
                    await copy_text_to_saved(
                        client, message, header="📥 Text post saved via link"
                    )
                    saved += 1
                except Exception:
                    failed += 1
            else:
                failed += 1
            continue
        outcome = await _process_message(
            message=message,
            account_id=account_id,
            manager=manager,
            session_factory=session_factory,
            force=True,
            capture_only_timed=False,
        )
        if outcome == "saved":
            saved += 1
        elif outcome == "duplicate":
            duplicate += 1
        else:
            failed += 1
        if index < len(ids) - 1 and settings.restricted_batch_delay_seconds > 0:
            await asyncio.sleep(settings.restricted_batch_delay_seconds)

    summary = f"✅ Link done: {saved} saved"
    if duplicate:
        summary += f", {duplicate} already saved"
    if failed:
        summary += f", {failed} not found/failed"
    summary += "."
    await _notify_owner(client, summary)


async def _process_message(
    *,
    message: Message,
    account_id: int,
    manager,
    session_factory: Callable[[], AsyncSession],
    force: bool,
    capture_only_timed: bool,
    capture_protected_content: bool = True,
) -> str:
    """Capture + save a single message for a given account.

    Args:
        force: True for explicit saves (/save, bare link, Saved Messages
            trigger) — bypasses the timed-only filter and always delivers
            a copy, including re-sending an already-saved post and
            retrying FAILED or interrupted (PENDING) records.
        capture_only_timed: If True, non-timed media is ignored — EXCEPT
            protected (noforwards) media when capture_protected_content
            is on: a protected post can never be fetched by forwarding
            later, so arrival is the only reliable chance to save it.

    Returns one of: "ignored", "saved", "duplicate", "failed".
    """
    # Step 1-2: classify (pure, no network)
    media_info = classify_message(message)
    if media_info is None:
        return "ignored"

    # Automatic mode saves only timed media unless configured otherwise.
    if (
        capture_only_timed
        and not force
        and not media_info.is_self_destruct
        and not (capture_protected_content and media_info.is_protected)
    ):
        return "ignored"

    # Step 3: get user_id from manager's registry
    managed = manager._clients.get(account_id)
    if managed is None:
        log.warning("event_account_not_in_manager", account_id=account_id)
        return "failed"
    user_id = managed.user_id

    # Extract video metadata once for consistency
    vid_duration = None
    vid_width = None
    vid_height = None
    from telethon.tl.types import DocumentAttributeVideo
    document = getattr(message, "document", None)
    if document:
        for attr in document.attributes:
            if isinstance(attr, DocumentAttributeVideo):
                vid_duration = int(attr.duration) if attr.duration else None
                vid_width = attr.w if attr.w else None
                vid_height = attr.h if attr.h else None
                break

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

    if not created and not force:
        # Automatic capture path only: an already-known message is a
        # duplicate to skip — this is what stops live-event replays and
        # album bursts from spamming Saved Messages with copies, and it
        # retries nothing (FAILED/PENDING records are RecoveryService's
        # and the next explicit save's business).
        #
        # An explicit save (force: /save, a bare link, or the Saved
        # Messages trigger) ALWAYS delivers a copy, even when this post
        # was saved before — the owner asked for it by hand, so it is
        # sent as-is with no duplicate check standing in the way. That
        # also transparently retries FAILED records and finishes
        # PENDING ones whose transfer was cut off mid-download.
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
                    duration=vid_duration,
                    width=vid_width,
                    height=vid_height,
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