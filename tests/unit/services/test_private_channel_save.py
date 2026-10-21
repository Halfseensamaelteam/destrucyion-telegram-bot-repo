"""Unit tests for /save link parsing and private-chat entity resolution."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl.types import PeerChannel

from app.services.private_channel_save import (
    SaveLinkError,
    _resolve_post_entity,
    parse_telegram_post_link,
)


def test_parse_public_post_link():
    assert parse_telegram_post_link("https://t.me/examplechannel/28") == (
        "examplechannel",
        28,
        False,
    )


def test_parse_private_post_link():
    assert parse_telegram_post_link("https://t.me/c/2691182672/1491") == (
        2691182672,
        1491,
        True,
    )


def test_parse_private_post_link_with_trailing_slash():
    assert parse_telegram_post_link("https://t.me/c/2691182672/1491/") == (
        2691182672,
        1491,
        True,
    )


@pytest.mark.parametrize(
    "link",
    [
        "https://t.me/c/not-a-channel/1491",
        "https://t.me/c/123/0",
        "https://example.com/channel/28",
        "https://t.me/examplechannel",
    ],
)
def test_reject_invalid_post_links(link):
    with pytest.raises(SaveLinkError):
        parse_telegram_post_link(link)


@pytest.mark.asyncio
async def test_private_entity_uses_cached_peer_when_available():
    client = MagicMock()
    entity = MagicMock()
    client.get_entity = AsyncMock(return_value=entity)

    result = await _resolve_post_entity(client, 2691182672, True)

    assert result is entity
    client.get_entity.assert_awaited_once_with(PeerChannel(2691182672))


@pytest.mark.asyncio
async def test_private_entity_falls_back_to_account_dialogs_when_not_cached():
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("entity not cached"))
    channel = MagicMock()
    channel.id = 2691182672
    dialog = MagicMock()
    dialog.entity = channel

    async def iter_dialogs():
        yield dialog

    client.iter_dialogs = iter_dialogs

    result = await _resolve_post_entity(client, 2691182672, True)

    assert result is channel


@pytest.mark.asyncio
async def test_private_entity_rejects_channel_not_visible_to_account():
    client = MagicMock()
    client.get_entity = AsyncMock(side_effect=ValueError("entity not cached"))

    async def iter_dialogs():
        if False:
            yield None

    client.iter_dialogs = iter_dialogs

    with pytest.raises(SaveLinkError, match="sudah bergabung"):
        await _resolve_post_entity(client, 2691182672, True)


@pytest.mark.asyncio
async def test_public_entity_uses_username_lookup():
    client = MagicMock()
    entity = MagicMock()
    client.get_entity = AsyncMock(return_value=entity)

    result = await _resolve_post_entity(client, "examplechannel", False)

    assert result is entity
    client.get_entity.assert_awaited_once_with("examplechannel")
