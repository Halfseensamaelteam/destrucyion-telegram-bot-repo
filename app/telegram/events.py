"""
app.telegram.events
~~~~~~~~~~~~~~~~~~~~
Telethon event handler factory for media capture and Saved Messages forwarding.

This module provides `make_handler_factory()` which returns a function
suitable for the `handler_factory` argument in `TelegramClientManager`.

Each account gets its own set of event handlers, bound to that account's
context. Handlers are isolated — a crash in one account's handler
does not affect other accounts.

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

from typing import Callable

from sqlalchemy.ext.asyncio import AsyncSession
from telethon import events
from telethon.tl.types import Message

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

        async def on_new_message(event: events.NewMessage.Event) -> None:
            """Fired for every new message received by this account."""
            try:
                await _handle_message(
                    event=event,
                    account_id=account_id,
                    manager=manager,
                    session_factory=session_factory,
                )
            except Exception as exc:
                log.error(
                    "event_handler_unhandled_error",
                    account_id=account_id,
                    error=str(exc),
                )

        return [
            (on_new_message, events.NewMessage()),
        ]

    return handler_factory


async def _handle_message(
    *,
    event: events.NewMessage.Event,
    account_id: int,
    manager,
    session_factory: Callable[[], AsyncSession],
) -> None:
    """Process a single new message for a given account.

    1. Classify media (pure, no network).
    2. If no capturable media → ignore silently.
    3. Get account's user_id from the manager's client registry.
    4. Create a DB session (isolated per event).
    5. Build CaptureContext from message metadata.
    6. Record via MediaCaptureService (idempotent).
    7. Log result; forwarding to Saved Messages deferred to Phase 9.
    """
    message: Message = event.message

    # Step 1-2: classify
    media_info = classify_message(message)
    if media_info is None:
        return

    # Step 3: get user_id from manager's registry
    managed = manager._clients.get(account_id)
    if managed is None:
        log.warning("event_account_not_in_manager", account_id=account_id)
        return
    user_id = managed.user_id

    # Capture context is built outside the DB session to keep network calls
    # (get_chat, get_sender) separate from the transaction.
    ctx, sender_info, chat_info = await _build_context(
        event=event,
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
        log.debug("media_already_known", account_id=account_id, record_id=record.id)
        return

    log.info(
        "media_captured",
        account_id=account_id,
        record_id=record.id,
        media_type=record.media_type,
        ttl=record.ttl_seconds,
        is_timed=media_info.is_self_destruct,
    )

    # Phase 9: forward to this account's own Saved Messages.
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
            await capture_svc.mark_failed(record, error="Client not available for forwarding")
            await session.commit()
        return

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
            await capture_svc.mark_saved(record, saved_message_id=result.saved_message_id)
            await session.commit()
        log.info(
            "saved_messages_saved",
            account_id=account_id,
            record_id=record.id,
            saved_msg_id=result.saved_message_id,
            was_resent=result.was_resent,
        )
    except SavedMessagesForwardError as fwd_err:
        log.error(
            "saved_messages_failed",
            account_id=account_id,
            record_id=record.id,
            error=str(fwd_err),
        )
        async with session_factory() as session:
            capture_svc = MediaCaptureService(session)
            await capture_svc.mark_failed(record, error=str(fwd_err))
            await session.commit()


async def _build_context(
    *,
    event: events.NewMessage.Event,
    message: Message,
    account_id: int,
    user_id: int,
    media_info,
) -> tuple[CaptureContext, SenderInfo, ChatInfo]:
    """Extract all metadata from the Telethon event into a CaptureContext.

    Returns (CaptureContext, SenderInfo, ChatInfo) so that the caller can
    pass SenderInfo/ChatInfo to SavedMessagesService without re-fetching.

    Uses app.telegram.metadata for robust sender/chat extraction.
    All failures are caught — metadata errors must never block capture.
    """
    source_chat_id = event.chat_id

    # Chat metadata — best effort
    chat_obj = None
    try:
        chat_obj = await event.get_chat()
    except Exception:
        pass

    chat_info = extract_chat(chat_obj, source_chat_id)

    # Sender metadata — best effort
    sender_obj = None
    try:
        sender_obj = await event.get_sender()
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
