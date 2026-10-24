"""
app.services.story
~~~~~~~~~~~~~~~~~~
Story fetching on one account's Telethon client.

Stories are NOT Bot API objects: the Bot API has no story download at
all, so — exactly like restricted posts — stories are fetched with the
requesting user's OWN connected MTProto client (the worker is the only
place such clients live). The connected account must be able to VIEW a
story for it to be downloadable: Telegram story privacy (Everyone /
Contacts / Close Friends) is enforced server-side and there is no way
around it. A story that has expired (past its 24h life and not pinned
as a highlight) is gone for everyone — GetStoriesByID simply returns
nothing for it.

API notes (Telethon >= 1.39, verified against the installed package):
  - Stories arrive as ``StoryItem`` objects. ``story.media`` is a plain
    ``MessageMediaPhoto`` / ``MessageMediaDocument`` — the same types
    ``client.download_media`` already accepts for messages. (Passing the
    StoryItem itself to ``download_media`` does NOT work; the media
    attribute must be passed.)
  - ``story.caption`` is a plain string (possibly empty). It is carried
    verbatim, per the standing caption policy (original caption only).
  - Single story: ``stories.GetStoriesByID`` (peer + [id]).
  - All active: ``stories.GetPeerStories`` (active stories) PLUS
    ``stories.GetPinnedStories`` (highlights, which persist past 24h),
    deduplicated by story id.
  - Raw story functions take an InputPeer, so the username is resolved
    first via ``client.get_input_entity``.
"""

from __future__ import annotations

from app.core.logging import get_logger
from app.services.restricted import NotMemberError, RestrictedContentError
from app.telegram.link_parser import StoryRef

log = get_logger(__name__)


def _extract_stories(result) -> list:
    """Pull the story list out of any stories-API result shape."""
    if result is None:
        return []
    if isinstance(result, list):
        return result
    stories = getattr(result, "stories", None)
    if isinstance(stories, list):
        return stories
    return []


def story_media_size_bytes(story) -> int | None:
    """Byte size of a story's media, or None when Telegram reports none.

    Documents carry their size directly; photos carry several sized
    variants, so the largest variant is the downloaded one. Unknown
    sizes are accounted at the big-file threshold by the caller (the
    conservative side, same as restricted downloads).
    """
    media = getattr(story, "media", None)
    if media is None:
        return None
    document = getattr(media, "document", None)
    if document is not None:
        size = getattr(document, "size", None)
        return size if isinstance(size, int) and size > 0 else None
    photo = getattr(media, "photo", None)
    if photo is not None:
        sizes = getattr(photo, "sizes", None) or []
        values = [
            getattr(variant, "size", None)
            for variant in sizes
            if isinstance(getattr(variant, "size", None), int)
            and getattr(variant, "size", None) > 0
        ]
        return max(values) if values else None
    return None


class StoryService:
    """Fetch stories on one account's client.

    One instance per (client, account) — never shared across accounts
    (CLAUDE.md §9 routing invariant, same as RestrictedContentService).
    """

    def __init__(self, client, account_id: int) -> None:
        self._client = client
        self._account_id = account_id

    async def _input_peer(self, username: str):
        """Resolve a story-link username to an InputPeer, or raise."""
        try:
            return await self._client.get_input_entity(username)
        except Exception as exc:
            raise self._translate_fetch_error(exc) from exc

    async def fetch_single(self, ref: StoryRef):
        """Fetch one story by id; raises when it is gone or unviewable."""
        from telethon.tl.functions.stories import GetStoriesByIDRequest

        if ref.story_id is None:
            raise RestrictedContentError("No story id given.")
        peer = await self._input_peer(ref.peer)
        try:
            result = await self._client(
                GetStoriesByIDRequest(peer=peer, id=[ref.story_id])
            )
        except Exception as exc:
            raise self._translate_fetch_error(exc) from exc
        stories = _extract_stories(result)
        if not stories:
            raise RestrictedContentError(
                "That story is no longer available — it may have expired "
                "(stories last 24 hours unless pinned as a highlight) or "
                "your connected account cannot view it."
            )
        return stories[0]

    async def fetch_all(self, ref: StoryRef) -> list:
        """Fetch all currently active stories of a peer.

        Active stories plus pinned/highlight stories, deduplicated by
        story id (a pinned story can also appear in the active list).
        An empty result is a clean "nothing right now", not an error —
        the caller turns it into a user-facing message.
        """
        from telethon.tl.functions.stories import (
            GetPeerStoriesRequest,
            GetPinnedStoriesRequest,
        )

        peer = await self._input_peer(ref.peer)
        try:
            active = await self._client(GetPeerStoriesRequest(peer=peer))
            pinned = await self._client(
                GetPinnedStoriesRequest(peer=peer, offset_id=0, limit=100)
            )
        except Exception as exc:
            raise self._translate_fetch_error(exc) from exc
        seen: set[int] = set()
        stories = []
        for story in _extract_stories(active) + _extract_stories(pinned):
            story_id = getattr(story, "id", None)
            if story_id is not None and story_id in seen:
                continue
            if story_id is not None:
                seen.add(story_id)
            stories.append(story)
        return stories

    def _translate_fetch_error(self, exc: Exception) -> RestrictedContentError:
        name = type(exc).__name__
        if name in ("ChannelPrivateError", "ChatIdInvalidError", "PeerIdInvalidError"):
            return NotMemberError(
                "Your connected account cannot view that account's stories "
                "(private account, or the story is limited to contacts / "
                "close friends your account is not part of)."
            )
        if name in ("UsernameNotOccupiedError", "UsernameInvalidError"):
            return RestrictedContentError("That username does not exist.")
        return RestrictedContentError(f"Could not fetch stories: {exc}")
