"""
tests/unit/telegram/test_story_parser.py
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
parse_story_link: story links only, never post/invite links.
"""

from app.telegram.link_parser import StoryRef, parse_link, parse_story_link


def test_single_story_link():
    ref = parse_story_link("https://t.me/dnrchmaa/s/7")
    assert ref == StoryRef(peer="dnrchmaa", story_id=7)
    assert not ref.all_active


def test_bare_username_means_all_active():
    ref = parse_story_link("https://t.me/dnrchmaa")
    assert ref == StoryRef(peer="dnrchmaa", story_id=None)
    assert ref.all_active


def test_story_link_inside_command_text():
    assert parse_story_link("/story https://t.me/dnrchmaa/s/7") == StoryRef(
        peer="dnrchmaa", story_id=7
    )


def test_story_link_with_query_string():
    assert parse_story_link("https://t.me/dnrchmaa/s/7?x=1") == StoryRef(
        peer="dnrchmaa", story_id=7
    )


def test_post_and_invite_links_are_not_stories():
    assert parse_story_link("https://t.me/chan/42") is None
    assert parse_story_link("https://t.me/c/1234567/47") is None
    assert parse_story_link("https://t.me/+AbCdEf") is None
    assert parse_story_link("https://t.me/joinchat/AbCdEf") is None


def test_malformed_story_links_rejected():
    assert parse_story_link("https://t.me/dnrchmaa/s/abc") is None
    assert parse_story_link("https://t.me/dnrchmaa/s/0") is None
    assert parse_story_link("https://t.me/dnrchmaa/s/") is None
    assert parse_story_link("https://t.me/s") is None
    assert parse_story_link("https://t.me/12345") is None
    assert parse_story_link("hello world") is None
    assert parse_story_link("") is None


def test_story_links_never_parse_as_posts():
    """The /save path must keep rejecting story links exactly as before."""
    assert parse_link("https://t.me/dnrchmaa/s/7") is None
    assert parse_link("https://t.me/dnrchmaa") is None
