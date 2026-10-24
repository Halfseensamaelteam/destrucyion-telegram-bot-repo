"""
app.telegram.link_parser
~~~~~~~~~~~~~~~~~~~~~~~~
Pure parsing of Telegram post / invite links — no network, no Telethon.

Supported link shapes (the same shapes the reference
Save-Restricted-Content bot accepts, plus forum-topic links it does not):

  Public post        https://t.me/<username>/<msg_id>
  Public topic post  https://t.me/<username>/<topic_id>/<msg_id>
  Private post       https://t.me/c/<internal_id>/<msg_id>
  Private topic post https://t.me/c/<internal_id>/<topic_id>/<msg_id>
  Bot-chat post      https://t.me/b/<bot_username>/<msg_id>
  Range              any post link above with <from>-<to> as the last part
                     e.g. https://t.me/c/1234567/100-110
  Invite             https://t.me/+<hash>  or  https://t.me/joinchat/<hash>

Why private links use ``/c/``: Telegram never exposes a username for a
private channel/group. The number after ``/c/`` is the channel's *bare* id;
the real peer id Telethon expects is ``-100<internal_id>``. That conversion
happens here, once, so no caller ever has to remember it.

Ranges are expanded here but CAPPED by the caller (settings): a range is a
batch download, and an uncapped range is a FloodWait / ban risk. This module
only refuses ranges that are reversed or absurdly large (> 1000) — the
tighter, operator-configurable cap lives in app.services.restricted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Hosts that carry real Telegram post links. Anything else is not a link
# we act on (a bare "t.me" mention in prose must not trigger a download).
_HOSTS = r"(?:t\.me|telegram\.me|telegram\.dog)"

_LINK_RE = re.compile(
    rf"https?://{_HOSTS}/(?P<path>[A-Za-z0-9_+\-/]+)(?:\?[^\s]*)?",
)

# An invite hash is the whole "username" segment for /+... and /joinchat/...
_INVITE_PREFIXES = ("+", "joinchat/")


@dataclass(frozen=True)
class PostRef:
    """A resolved reference to one or more messages in a chat.

    Attributes:
        peer: What Telethon's ``get_messages``/``get_entity`` accepts —
            a username string for public/bot chats, or the full
            ``-100...`` integer peer id for private (``/c/``) chats.
        message_ids: Message IDs to fetch, in ascending order. More than
            one only for an explicit ``from-to`` range link.
        is_private: True for ``/c/`` links (account MUST already be a
            member — a private peer cannot be resolved from outside).
        topic_id: Forum topic id when the link is a topic link, else None.
            The topic id is NOT a message id; dropping this distinction is
            the classic bug that fetches the wrong message.
    """

    peer: str | int
    message_ids: tuple[int, ...]
    is_private: bool = False
    topic_id: int | None = None

    @property
    def first_message_id(self) -> int:
        return self.message_ids[0]


@dataclass(frozen=True)
class InviteRef:
    """A private-chat invite link (``t.me/+...`` / ``t.me/joinchat/...``)."""

    invite_hash: str


@dataclass(frozen=True)
class StoryRef:
    """A reference to Telegram stories, handled ONLY by the /story command.

    Attributes:
        peer: Username string Telethon's ``get_input_entity`` accepts.
            Stories have no private-link form: a peer without a username
            has no public story link, so only usernames occur here.
        story_id: One story id for ``/<username>/s/<story_id>``, or None
            for a bare ``/<username>`` link, which means "all currently
            active stories of this peer" (active + pinned/highlight).

    Deliberately separate from PostRef: /save must never treat a story
    link as a post link, and /story must never treat a post link as a
    story link. The two commands share nothing but the queue row shape.
    """

    peer: str
    story_id: int | None = None

    @property
    def all_active(self) -> bool:
        return self.story_id is None


def _parse_id_or_range(segment: str) -> tuple[int, ...] | None:
    """Parse ``123`` or ``100-110`` into a tuple of message ids.

    Returns None for anything that is not a sane id/range. A range whose
    span exceeds 1000 is rejected here as certainly-a-mistake; the real
    batch cap (default 20) is enforced by the service layer.
    """
    segment = segment.strip()
    if segment.isdigit():
        value = int(segment)
        return (value,) if value > 0 else None
    if "-" in segment:
        left, _, right = segment.partition("-")
        if left.strip().isdigit() and right.strip().isdigit():
            start, end = int(left), int(right)
            if 0 < start <= end and (end - start) < 1000:
                return tuple(range(start, end + 1))
    return None


def parse_link(text: str) -> PostRef | InviteRef | None:
    """Parse the FIRST Telegram link found in ``text``.

    Returns a PostRef for post links, an InviteRef for invite links, or
    None when the text holds no usable Telegram link. Pure function —
    safe to call on any user input.
    """
    if not text:
        return None
    match = _LINK_RE.search(text)
    if not match:
        return None

    path = match.group("path").strip("/")
    if not path:
        return None

    # --- Invite links -------------------------------------------------
    for prefix in _INVITE_PREFIXES:
        if path.startswith(prefix):
            invite_hash = path[len(prefix):].strip("/")
            # joinchat hash may itself start with "+" in old links; the
            # ImportChatInvite API wants the bare hash.
            invite_hash = invite_hash.lstrip("+")
            return InviteRef(invite_hash=invite_hash) if invite_hash else None
    if path.startswith("+"):
        invite_hash = path[1:].strip("/")
        return InviteRef(invite_hash=invite_hash) if invite_hash else None

    parts = [p for p in path.split("/") if p != ""]

    # --- Private post: /c/<internal>/<msg> or /c/<internal>/<topic>/<msg>
    if parts[0] == "c" and len(parts) in (3, 4) and parts[1].isdigit():
        ids = _parse_id_or_range(parts[-1])
        if ids is None:
            return None
        peer = int(f"-100{parts[1]}")
        topic_id = int(parts[2]) if len(parts) == 4 and parts[2].isdigit() else None
        # A 4-part link whose middle part is not numeric is not a topic
        # link we understand — refuse rather than guess.
        if len(parts) == 4 and topic_id is None:
            return None
        return PostRef(peer=peer, message_ids=ids, is_private=True, topic_id=topic_id)

    # --- Bot-chat post: /b/<bot_username>/<msg>
    if parts[0] == "b" and len(parts) == 3:
        ids = _parse_id_or_range(parts[2])
        if ids is None:
            return None
        return PostRef(peer=parts[1], message_ids=ids, is_private=False)

    # --- Public post: /<username>/<msg> or /<username>/<topic>/<msg>
    if len(parts) in (2, 3):
        username = parts[0]
        # Single-letter / reserved segments are not usernames; a username
        # is also never purely numeric (that would be a malformed /c/).
        if username in ("c", "b", "s") or username.isdigit():
            return None
        ids = _parse_id_or_range(parts[-1])
        if ids is None:
            return None
        topic_id = int(parts[1]) if len(parts) == 3 and parts[1].isdigit() else None
        if len(parts) == 3 and topic_id is None:
            return None
        return PostRef(peer=username, message_ids=ids, is_private=False, topic_id=topic_id)

    return None


def find_post_or_invite(text: str) -> PostRef | InviteRef | None:
    """Alias kept readable at call sites that scan whole messages."""
    return parse_link(text)


def parse_story_link(text: str) -> StoryRef | None:
    """Parse the FIRST Telegram story link found in ``text``.

    Accepted shapes (only these — post links, invite links and anything
    else return None, so /story never swallows a /save link):

      Single story   https://t.me/<username>/s/<story_id>
      All active     https://t.me/<username>

    The bare-username form is unambiguous for /story: a bare username
    carries no message id, so it can never be a post link, and story
    links never use the /c/ or /b/ prefixes. Pure function — safe to
    call on any user input.
    """
    if not text:
        return None
    match = _LINK_RE.search(text)
    if not match:
        return None

    path = match.group("path").strip("/")
    if not path:
        return None

    # Invites are not stories.
    for prefix in _INVITE_PREFIXES:
        if path.startswith(prefix):
            return None
    if path.startswith("+"):
        return None

    parts = [p for p in path.split("/") if p != ""]

    # Single story: /<username>/s/<story_id>
    if len(parts) == 3 and parts[1] == "s":
        username, story_id = parts[0], parts[2]
        if username in ("c", "b", "s") or username.isdigit():
            return None
        if not story_id.isdigit() or int(story_id) <= 0:
            return None
        return StoryRef(peer=username, story_id=int(story_id))

    # All active stories: bare /<username>
    if len(parts) == 1:
        username = parts[0]
        if username in ("c", "b", "s") or username.isdigit():
            return None
        return StoryRef(peer=username, story_id=None)

    return None
