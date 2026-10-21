"""
tests/unit/telegram/test_link_parser.py
Unit tests for app.telegram.link_parser — pure link parsing, no network.
"""

import pytest

from app.telegram.link_parser import InviteRef, PostRef, parse_link


def test_public_post():
    ref = parse_link("https://t.me/somechannel/123")
    assert isinstance(ref, PostRef)
    assert ref.peer == "somechannel"
    assert ref.message_ids == (123,)
    assert ref.is_private is False
    assert ref.topic_id is None


def test_public_post_inside_text():
    ref = parse_link("save this please https://t.me/somechannel/42 thanks")
    assert isinstance(ref, PostRef)
    assert ref.message_ids == (42,)


def test_private_post_converts_to_full_peer_id():
    ref = parse_link("https://t.me/c/1234567/890")
    assert isinstance(ref, PostRef)
    # The whole point of /c/ parsing: bare id -> -100<id>
    assert ref.peer == -1001234567
    assert ref.message_ids == (890,)
    assert ref.is_private is True


def test_private_topic_post():
    ref = parse_link("https://t.me/c/1234567/7/890")
    assert isinstance(ref, PostRef)
    assert ref.peer == -1001234567
    assert ref.topic_id == 7
    assert ref.message_ids == (890,)


def test_public_topic_post():
    ref = parse_link("https://t.me/somegroup/3/99")
    assert isinstance(ref, PostRef)
    assert ref.peer == "somegroup"
    assert ref.topic_id == 3
    assert ref.message_ids == (99,)


def test_bot_chat_post():
    ref = parse_link("https://t.me/b/somebot/4321")
    assert isinstance(ref, PostRef)
    assert ref.peer == "somebot"
    assert ref.message_ids == (4321,)


def test_range_expands():
    ref = parse_link("https://t.me/c/1234567/100-103")
    assert isinstance(ref, PostRef)
    assert ref.message_ids == (100, 101, 102, 103)


def test_range_with_spaces_and_single_query():
    ref = parse_link("https://t.me/chan/100 - 102")
    # Spaces around the dash live OUTSIDE the URL token, so only the
    # first id is part of the link — the parser must not invent a range.
    assert isinstance(ref, PostRef)
    assert ref.message_ids == (100,)


def test_query_string_is_ignored():
    ref = parse_link("https://t.me/chan/55?single")
    assert isinstance(ref, PostRef)
    assert ref.message_ids == (55,)


def test_invite_plus():
    ref = parse_link("https://t.me/+AbCdEfGhIjK")
    assert isinstance(ref, InviteRef)
    assert ref.invite_hash == "AbCdEfGhIjK"


def test_invite_joinchat():
    ref = parse_link("https://t.me/joinchat/AbCdEfGhIjK")
    assert isinstance(ref, InviteRef)
    assert ref.invite_hash == "AbCdEfGhIjK"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "hello world",
        "https://example.com/chan/123",
        "https://t.me/chan",               # no message id
        "https://t.me/chan/abc",           # non-numeric id
        "https://t.me/c/notanumber/123",   # malformed private link
        "https://t.me/c/123/110-100",      # reversed range
        "https://t.me/12345/678",          # numeric 'username'
    ],
)
def test_unusable_links_return_none(text):
    assert parse_link(text) is None
