"""
app.bot.handlers.story
~~~~~~~~~~~~~~~~~~~~~~
/story — submit a Telegram story link for downloading.

Stories are NOT part of /save: /save stays post/invite-only, and this
command is the only path that accepts story links. The bot process
holds NO Telethon client (only the worker does), so this handler never
tries to fetch anything itself. It validates the link, checks the user
has a connected account + an active subscription (the same gates the
worker enforces), then writes a PENDING LinkRequest row exactly like
/save does. The worker's LinkRequestProcessor drains it with the user's
OWN account client and the copy lands in that account's Saved Messages;
the worker also pings this bot chat when it finishes, editing the one
status message created here.

Usage:
    /story https://t.me/<username>/s/<story_id>   (one story)
    /story https://t.me/<username>                (all active stories)

The one-active-request gate that /save has does NOT apply here: /story
is a separate command with its own queue rows, and the worker's
per-account lock still guarantees only one download actually runs per
account at a time.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.core.logging import get_logger
from app.db.models.telegram_account import TelegramAccountStatus
from app.db.models.user import User
from app.db.repositories.link_request_repo import LinkRequestRepository
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.subscription import SubscriptionService
from app.telegram.link_parser import parse_story_link

log = get_logger(__name__)

HELP_HINT = (
    "Send me a story link like:\n"
    "• /story https://t.me/<username>/s/7  (one story)\n"
    "• /story https://t.me/<username>  (all active stories)\n"
    "The story must be viewable by your connected account: public "
    "stories work for anyone, but contacts-only or close-friends stories "
    "only work if your account is allowed to see them. Expired stories "
    "cannot be downloaded."
)


@with_db_and_user
async def story_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /story <link>."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    # Strip the /story command itself when present.
    if text.startswith("/story"):
        text = text[len("/story"):].strip()

    parsed = parse_story_link(text)
    if parsed is None:
        await update.message.reply_text(
            "⚠️ That does not look like a Telegram story link.\n\n" + HELP_HINT
        )
        return

    account_repo = TelegramAccountRepository(session)
    account = await account_repo.get_by_user_id(user.id)
    if account is None or account.status == TelegramAccountStatus.DISCONNECTED:
        await update.message.reply_text(
            "⚠️ You have no connected Telegram account yet. Use /connect first — "
            "the story is fetched with YOUR account, because it must be allowed "
            "to view that account's stories."
        )
        return

    subscription = SubscriptionService(session)
    if not await subscription.check_active(user.id):
        await update.message.reply_text(
            "⚠️ You need an active subscription to download stories. "
            "See /subscription."
        )
        return

    # ONE status message per request: the bot sends "Queued" now and the
    # worker EDITS this same message as the state changes (Working ->
    # Done/Failed) instead of posting a new message per state. Its ids
    # ride along on the queue row so the worker can find it.
    status_msg = await update.message.reply_text("⏳ Queued…")
    link_repo = LinkRequestRepository(session)
    await link_repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link=text,
        notify_chat_id=status_msg.chat_id,
        notify_message_id=status_msg.message_id,
    )
    log.info("story_request_queued", user_id=user.id, account_id=account.id)
