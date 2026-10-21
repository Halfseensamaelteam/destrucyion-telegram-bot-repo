"""
app.bot.handlers.restricted
~~~~~~~~~~~~~~~~~~~~~~~~~~~
/save — submit a Telegram post/invite link for restricted-content saving.

The bot process holds NO Telethon client (only the worker does), so this
handler never tries to fetch anything itself. It validates the link,
checks the user has a connected account + an active subscription (the
same gates the worker enforces), then writes a PENDING LinkRequest row.
The worker's LinkRequestProcessor drains it with the user's OWN account
client and the copy lands in that account's Saved Messages; the worker
also pings this bot chat when it finishes.

Usage:
    /save https://t.me/c/1234567/890          (private/restricted post)
    /save https://t.me/channel/123           (public post)
    /save https://t.me/c/1234567/100-110     (range, capped in worker)
    /save https://t.me/+AbCdEfGh             (join first, then /save the post)

A bare t.me link sent as a plain message is treated the same as /save.
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
from app.telegram.link_parser import parse_link

log = get_logger(__name__)

HELP_HINT = (
    "Send me a post link like:\n"
    "• /save https://t.me/c/1234567/890\n"
    "• /save https://t.me/channel/123\n"
    "• /save https://t.me/c/1234567/100-110 (range)\n"
    "For a private channel your connected account must be a member — "
    "if it is not, first send its invite link: /save https://t.me/+AbCdEfGh"
)


@with_db_and_user
async def save_link_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /save <link> and bare t.me link messages."""
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    # Strip the /save command itself when present.
    if text.startswith("/save"):
        text = text[len("/save"):].strip()

    parsed = parse_link(text)
    if parsed is None:
        await update.message.reply_text(
            "⚠️ That does not look like a Telegram post or invite link.\n\n" + HELP_HINT
        )
        return

    account_repo = TelegramAccountRepository(session)
    account = await account_repo.get_by_user_id(user.id)
    if account is None or account.status == TelegramAccountStatus.DISCONNECTED:
        await update.message.reply_text(
            "⚠️ You have no connected Telegram account yet. Use /connect first — "
            "the link is fetched with YOUR account, because it must be a member "
            "of the private/restricted chat."
        )
        return

    subscription = SubscriptionService(session)
    if not await subscription.check_active(user.id):
        await update.message.reply_text(
            "⚠️ You need an active subscription to save restricted content. "
            "See /subscription."
        )
        return

    link_repo = LinkRequestRepository(session)
    await link_repo.create(
        user_id=user.id,
        telegram_account_id=account.id,
        raw_link=text,
    )
    log.info("link_request_queued", user_id=user.id, account_id=account.id)
    await update.message.reply_text(
        "⏳ Queued. Your account will fetch it in the background and the "
        "copy will appear in your Saved Messages. I will confirm here when "
        "it is done."
    )


async def save_link_message(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Plain-message variant: only act when the text IS a t.me link.

    Registered for private text messages; silently ignores everything
    that is not a Telegram post/invite link so normal conversation with
    the bot is never hijacked. The real work (incl. DB/user injection)
    is in save_link_command.
    """
    if not update.message or not update.message.text:
        return
    if parse_link(update.message.text) is None:
        return
    await save_link_command(update, context)
