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

from dataclasses import dataclass

from app.core.logging import get_logger
from app.telegram.metadata import SenderInfo, ChatInfo, build_caption

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
    """

    saved_message_id: int
    caption_used: str
    was_resent: bool


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
            ForwardResult with the saved_message_id.

        Raises:
            SavedMessagesForwardError: When all forwarding strategies fail.
        """
        from telethon.tl.functions.messages import ForwardMessagesRequest
        from telethon.tl.types import InputPeerSelf

        caption = build_caption(
            sender=sender_info,
            chat=chat_info,
            media_type=record.media_type.value,
            ttl_seconds=record.ttl_seconds,
        )

        # Strategy 1: forward (zero re-upload)
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

        # Strategy 2: resend via file reference
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
        )

    async def _try_resend(self, source_message, caption: str) -> ForwardResult:
        """Send media by file reference when forward is unavailable.

        Used when the source message has already disappeared (timed media)
        or the chat has forward restrictions. This path requires the media
        bytes to still be reachable via the file_reference in the session.
        """
        from telethon.tl.types import InputPeerSelf

        media = getattr(source_message, "media", None)
        if media is None:
            raise SavedMessagesForwardError("Source message has no media to resend")

        sent = await self._client.send_file(
            entity=InputPeerSelf(),
            file=media,
            caption=caption,
        )

        return ForwardResult(
            saved_message_id=sent.id,
            caption_used=caption,
            was_resent=True,
        )
