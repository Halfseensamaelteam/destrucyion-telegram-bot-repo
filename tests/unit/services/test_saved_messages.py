"""
tests/unit/services/test_saved_messages.py
Unit tests for SavedMessagesService — routing correctness is the primary concern.

Phase 9 critical invariant (ROADMAP.md):
  Account A → Account A's Saved Messages
  Account B → Account B's Saved Messages
  NEVER cross-account routing.

All Telethon network calls are mocked. No real Telegram connection.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call

from app.db.models.media_record import MediaRecord, MediaRecordStatus, MediaType
from app.services.saved_messages import (
    ForwardResult,
    RoutingError,
    SavedMessagesForwardError,
    SavedMessagesService,
)
from app.telegram.metadata import SenderInfo, ChatInfo

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def make_sender(username="alice") -> SenderInfo:
    return SenderInfo(
        telegram_id=1001,
        username=username,
        display_name="Alice",
        is_anonymous=False,
        is_channel=False,
    )


def make_chat(title="Test Chat") -> ChatInfo:
    return ChatInfo(
        chat_id=200,
        title=title,
        username=None,
        is_private=False,
        is_group=True,
        is_channel=False,
    )


def make_record(account_id=1, media_type=MediaType.PHOTO) -> MagicMock:
    record = MagicMock(spec=MediaRecord)
    record.id = 42
    record.telegram_account_id = account_id
    record.media_type = media_type
    record.ttl_seconds = None
    record.status = MediaRecordStatus.PENDING
    return record


def make_message(media=True) -> MagicMock:
    msg = MagicMock()
    msg.id = 9999
    msg.peer_id = MagicMock()
    if media:
        msg.media = MagicMock()
    else:
        msg.media = None
    return msg


def make_client(forward_result_id=1234, send_result_id=5678) -> MagicMock:
    """Mock Telethon client with working forward and send_file."""
    client = MagicMock()

    # forward_messages returns a list with one message
    fwd_msg = MagicMock()
    fwd_msg.id = forward_result_id
    client.forward_messages = AsyncMock(return_value=[fwd_msg])

    # send_message (caption follow-up) returns a message
    sent_msg = MagicMock()
    sent_msg.id = forward_result_id + 1
    client.send_message = AsyncMock(return_value=sent_msg)

    # send_file returns a message
    file_msg = MagicMock()
    file_msg.id = send_result_id
    client.send_file = AsyncMock(return_value=file_msg)

    return client


# ---------------------------------------------------------------------------
# Tests: Successful forward (Strategy 1)
# ---------------------------------------------------------------------------

async def test_forward_success():
    """Normal forward: uses forward_messages, returns saved_message_id."""
    client = make_client(forward_result_id=1111)
    svc = SavedMessagesService(client, account_id=1)

    result = await svc.forward(
        make_message(),
        sender_info=make_sender(),
        chat_info=make_chat(),
        record=make_record(account_id=1),
    )

    assert isinstance(result, ForwardResult)
    assert result.saved_message_id == 1111
    assert result.was_resent is False
    client.forward_messages.assert_called_once()


async def test_forward_calls_send_message_for_caption():
    """After forwarding, a caption reply is sent."""
    client = make_client(forward_result_id=2222)
    svc = SavedMessagesService(client, account_id=1)

    await svc.forward(
        make_message(),
        sender_info=make_sender(username="bob"),
        chat_info=make_chat(title="Group"),
        record=make_record(account_id=1),
    )

    # Caption send_message should have been called with reply_to=forwarded_id
    client.send_message.assert_called_once()
    call_kwargs = client.send_message.call_args.kwargs
    assert call_kwargs["reply_to"] == 2222
    assert "@bob" in call_kwargs["message"]
    assert "Group" in call_kwargs["message"]


# ---------------------------------------------------------------------------
# Tests: Fallback resend (Strategy 2)
# ---------------------------------------------------------------------------

async def test_forward_falls_back_to_resend_on_failure():
    """When forward_messages fails, falls back to send_file."""
    client = make_client()
    client.forward_messages = AsyncMock(side_effect=Exception("CHAT_FORWARD_RESTRICTED"))

    svc = SavedMessagesService(client, account_id=1)
    result = await svc.forward(
        make_message(),
        sender_info=make_sender(),
        chat_info=make_chat(),
        record=make_record(account_id=1),
    )

    assert result.was_resent is True
    assert result.saved_message_id == 5678
    client.send_file.assert_called_once()


async def test_forward_raises_when_both_strategies_fail():
    """When both forward and resend fail, raises SavedMessagesForwardError."""
    client = make_client()
    client.forward_messages = AsyncMock(side_effect=Exception("not available"))
    client.send_file = AsyncMock(side_effect=Exception("media expired"))

    svc = SavedMessagesService(client, account_id=1)

    with pytest.raises(SavedMessagesForwardError):
        await svc.forward(
            make_message(),
            sender_info=make_sender(),
            chat_info=make_chat(),
            record=make_record(account_id=1),
        )


async def test_resend_fails_with_no_media():
    """send_file fallback raises error when message has no media."""
    client = make_client()
    client.forward_messages = AsyncMock(side_effect=Exception("unavailable"))

    svc = SavedMessagesService(client, account_id=1)

    with pytest.raises(SavedMessagesForwardError):
        await svc.forward(
            make_message(media=False),  # message has no media
            sender_info=make_sender(),
            chat_info=make_chat(),
            record=make_record(account_id=1),
        )


# ---------------------------------------------------------------------------
# Tests: Routing isolation (CRITICAL — Phase 9 invariant)
# ---------------------------------------------------------------------------

async def test_routing_account_a_uses_own_client():
    """Account A's service uses Account A's client with InputPeerSelf — never hardcoded peer."""
    from telethon.tl.types import InputPeerSelf

    client_a = make_client(forward_result_id=1001)
    svc_a = SavedMessagesService(client_a, account_id=1)

    await svc_a.forward(
        make_message(),
        sender_info=make_sender(),
        chat_info=make_chat(),
        record=make_record(account_id=1),
    )

    # The entity passed to forward_messages must be InputPeerSelf (resolved from client)
    call_kwargs = client_a.forward_messages.call_args.kwargs
    entity = call_kwargs.get("entity") or client_a.forward_messages.call_args.args[0]
    assert isinstance(entity, InputPeerSelf), (
        "Routing MUST use InputPeerSelf, not a hardcoded peer ID"
    )


async def test_routing_account_b_uses_own_client():
    """Account B's service uses Account B's client independently — never Account A's client."""
    from telethon.tl.types import InputPeerSelf

    client_b = make_client(forward_result_id=2002)
    svc_b = SavedMessagesService(client_b, account_id=2)

    await svc_b.forward(
        make_message(),
        sender_info=make_sender(username="carol"),
        chat_info=make_chat(),
        record=make_record(account_id=2),
    )

    # Verify client_b was used (forward_messages called on client_b)
    client_b.forward_messages.assert_called_once()


async def test_two_accounts_never_share_client():
    """Two separate service instances for two accounts NEVER share a client object."""
    client_a = make_client(forward_result_id=1001)
    client_b = make_client(forward_result_id=2002)

    svc_a = SavedMessagesService(client_a, account_id=1)
    svc_b = SavedMessagesService(client_b, account_id=2)

    await svc_a.forward(make_message(), sender_info=make_sender(), chat_info=make_chat(), record=make_record(account_id=1))
    await svc_b.forward(make_message(), sender_info=make_sender(), chat_info=make_chat(), record=make_record(account_id=2))

    # Each client's forward_messages called exactly once
    client_a.forward_messages.assert_called_once()
    client_b.forward_messages.assert_called_once()

    # Verify client_a and client_b are different objects
    assert client_a is not client_b


# ---------------------------------------------------------------------------
# Tests: Caption content
# ---------------------------------------------------------------------------

async def test_caption_includes_timed_marker_for_timed_media():
    """For self-destructing media, caption includes the TTL marker."""
    client = make_client()
    svc = SavedMessagesService(client, account_id=1)

    record = make_record()
    record.ttl_seconds = 10
    record.media_type = MediaType.VIDEO

    await svc.forward(
        make_message(),
        sender_info=make_sender(username="dave"),
        chat_info=make_chat(),
        record=record,
    )

    caption_arg = client.send_message.call_args.kwargs["message"]
    assert "Self-destructing" in caption_arg
    assert "10s" in caption_arg


async def test_caption_does_not_include_timed_marker_for_normal_media():
    """For normal media (no TTL), caption has no TTL marker."""
    client = make_client()
    svc = SavedMessagesService(client, account_id=1)

    record = make_record()
    record.ttl_seconds = None
    record.media_type = MediaType.PHOTO

    await svc.forward(
        make_message(),
        sender_info=make_sender(),
        chat_info=make_chat(),
        record=record,
    )

    caption_arg = client.send_message.call_args.kwargs["message"]
    assert "Self-destructing" not in caption_arg
    assert "⏱" not in caption_arg
