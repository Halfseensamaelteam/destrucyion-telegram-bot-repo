"""
app.bot.handlers.account
~~~~~~~~~~~~~~~~~~~~~~~~
Account management commands: /accounts, /connect, /disconnect.

Authentication supports both QR login and secure browser phone login. /connect
lets the user choose the method. QR login keeps the existing flow; phone login
opens the short-lived HTTPS browser flow from app.bot.handlers.phone_login.

IMPORTANT — these are ordinary customer-facing commands, NOT admin-only.
Any authenticated application user may connect their own single Telegram
account (CLAUDE.md §12.3). Do NOT apply @admin_required here — that
decorator is for actual /admin/* commands only.

The QR login itself runs as a detached background task rather than inside
the ConversationHandler, because it can take up to several minutes
(auto-refreshing the QR token, optionally waiting on a 2FA reply) and must
hold ONE continuously-connected Telethon client the whole time — see
app/services/account.py for why. The background task opens its OWN
database session via `_get_session_factory()`, since the ConversationHandler
request's session is already closed by the time this task runs.

The optional 2FA password step happens OUTSIDE the ConversationHandler: a
plain-text message from the user is captured by `maybe_capture_2fa_password`
(registered in an earlier handler group in app/bot/bot.py), which resolves a
pending asyncio.Future that perform_qr_login() is awaiting. No live Telethon
object is ever stored in `context.user_data` — such objects are not
picklable and this bot's ConversationHandler uses persistent storage.
"""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, Update
from telegram.ext import (
    ApplicationHandlerStop,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from app.bot.dependencies import with_db_and_user
from app.bot.qr_utils import render_qr_png
from app.core.logging import get_logger
from app.db.models.user import User
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.db.session import _get_session_factory
from app.bot.handlers.phone_login import connect_phone_command
from app.services.account import (
    AccountAlreadyExistsError,
    AccountService,
    AuthError,
    QrLoginCallbacks,
)

log = get_logger(__name__)

CONNECT_METHOD = 1
ENTER_API_ID = 2
ENTER_API_HASH = 3

# chat_id -> asyncio.Future[str], resolved by maybe_capture_2fa_password()
# when the user replies with their 2FA password during an in-progress QR
# login for that chat. Module-level and process-local by design.
_pending_2fa_futures: dict[int, "asyncio.Future[str]"] = {}


@with_db_and_user
async def accounts_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /accounts. Shows the user's single connected account, if any."""
    repo = TelegramAccountRepository(session)
    account = await repo.get_by_user_id(user.id)

    if account is None:
        text = "You have no connected account. Use /connect to add one."
    else:
        label = account.username or account.telegram_user_id or account.id
        text = (
            "📱 *Your Connected Account:*\n\n"
            f"🔹 `{label}`\n"
            f"   Status: {account.status.value.upper()}\n\n"
            "To remove it, use /disconnect."
        )

    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /connect — collect this account's own api_id/api_hash, then QR login
# ---------------------------------------------------------------------------

@with_db_and_user
async def connect_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Entry point for /connect."""
    repo = TelegramAccountRepository(session)
    existing = await repo.get_by_user_id(user.id)
    if existing is not None and existing.status.value != "disconnected":
        await update.message.reply_text(
            "You already have a Telegram account connected (or a connection "
            "in progress). Use /disconnect first if you want to connect a "
            "different one."
        )
        return ConversationHandler.END

    # If this user previously connected and disconnected (rather than
    # having their Telegram session revoked/unlinked), try to resume that
    # exact session first — no need to make them scan a QR code again.
    if existing is not None and existing.session_ciphertext:
        await update.message.reply_text("🔄 Checking your previous session...")
        svc = AccountService(session)
        try:
            resumed = await svc.try_resume_session(account_id=existing.id, user_id=user.id)
            await session.commit()
        except Exception:
            await session.rollback()
            resumed = False

        if resumed:
            await update.message.reply_text(
                "✅ Reconnected using your previous session — no QR code needed!"
            )
            return ConversationHandler.END

        await update.message.reply_text(
            "⚠️ Your previous session is no longer valid. This usually means "
            "the linked session was removed from Telegram (check Settings → "
            "Devices in your Telegram app). Let's set up a new connection."
        )
        # api_id/api_hash are typically unaffected by a revoked session —
        # reuse what's on file and go straight to QR rather than re-asking.
        if existing.api_id and existing.api_hash_ciphertext:
            chat_id = update.effective_chat.id
            await context.bot.send_message(chat_id, "⏳ Generating your QR code...")
            asyncio.create_task(
                _run_qr_login_task(
                    bot=context.bot, chat_id=chat_id, account_id=existing.id, user_id=user.id
                )
            )
            return ConversationHandler.END
        # else: fall through to ask for api_id/api_hash again below

    keyboard = [
        [InlineKeyboardButton("📱 Phone Login", callback_data="connect_phone")],
        [InlineKeyboardButton("🔳 QR Login", callback_data="connect_qr")],
    ]
    await update.message.reply_text(
        "🔐 Choose how you want to connect your Telegram account:\n\n"
        "📱 *Phone Login* — sign in with your phone number and Telegram code "
        "in the secure HTTPS browser page.\n"
        "🔳 *QR Login* — scan a Telegram QR code from your Telegram app.",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    context.user_data["user_id"] = user.id
    return CONNECT_METHOD




@with_db_and_user
async def connect_qr_choice(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Start the existing QR login flow after the user chooses QR."""
    query = update.callback_query
    await query.answer()

    repo = TelegramAccountRepository(session)
    existing = await repo.get_by_user_id(user.id)
    if existing is not None and existing.status.value != "disconnected":
        await query.edit_message_text(
            "You already have a connected account (or a connection is in progress). "
            "Use /disconnect first."
        )
        context.user_data.clear()
        return ConversationHandler.END

    await query.edit_message_text(
        "Let's connect your Telegram account via QR login.\n\n"
        "First, you'll need YOUR OWN api_id and api_hash from "
        "https://my.telegram.org (API development tools). Every account "
        "brings its own credentials.\n\n"
        "Please send your *api_id* (numbers only).\n\n"
        "Send /cancel to stop at any time.",
        parse_mode="Markdown",
    )
    return ENTER_API_ID


@with_db_and_user
async def connect_api_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    raw = update.message.text.strip()
    if not raw.isdigit():
        await update.message.reply_text("api_id must be numbers only. Try again:")
        return ENTER_API_ID

    context.user_data["api_id"] = int(raw)
    await update.message.reply_text(
        "Got it. Now send your *api_hash* (a 32-character string).",
        parse_mode="Markdown",
    )
    return ENTER_API_HASH


@with_db_and_user
async def connect_api_hash(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Handle api_hash input, create/reset the account, then launch QR login."""
    api_hash = update.message.text.strip()
    api_id = context.user_data.get("api_id")

    # Sensitive credential — remove it from chat history, same treatment as
    # a 2FA password.
    try:
        await update.message.delete()
    except Exception:
        pass

    svc = AccountService(session)
    try:
        account = await svc.create_or_reset_account(
            user_id=user.id, api_id=api_id, api_hash=api_hash
        )
        await session.commit()
    except AccountAlreadyExistsError as exc:
        await update.effective_chat.send_message(f"❌ {exc}")
        return ConversationHandler.END
    except Exception as exc:
        await session.rollback()
        await update.effective_chat.send_message(f"❌ Failed to save credentials: {exc}")
        return ConversationHandler.END

    chat_id = update.effective_chat.id
    await context.bot.send_message(
        chat_id, "⏳ Generating your QR code — this may take a moment..."
    )

    # Detached background task — see module docstring for why.
    asyncio.create_task(
        _run_qr_login_task(bot=context.bot, chat_id=chat_id, account_id=account.id, user_id=user.id)
    )

    context.user_data.clear()
    return ConversationHandler.END


async def connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("Connection cancelled.")
    return ConversationHandler.END


async def _run_qr_login_task(bot: Any, chat_id: int, account_id: int, user_id: int) -> None:
    """Background task: generate/refresh the QR code and drive perform_qr_login.

    Opens its OWN database session — the ConversationHandler's session is
    already closed by the time this runs, since it can run for minutes.
    """
    session_factory = _get_session_factory()
    qr_message_id: int | None = None

    async def on_qr_ready(url: str) -> None:
        nonlocal qr_message_id
        photo = render_qr_png(url)
        caption = (
            "📷 Scan this QR code with your OWN Telegram app:\n"
            "Settings → Devices → Link Desktop Device\n\n"
            "This code refreshes automatically if it expires before you scan it."
        )
        if qr_message_id is None:
            msg = await bot.send_photo(chat_id, photo=photo, caption=caption)
            qr_message_id = msg.message_id
        else:
            try:
                await bot.edit_message_media(
                    chat_id=chat_id,
                    message_id=qr_message_id,
                    media=InputMediaPhoto(photo, caption=caption),
                )
            except Exception:
                msg = await bot.send_photo(chat_id, photo=photo, caption=caption)
                qr_message_id = msg.message_id

    async def get_2fa_password() -> str:
        loop = asyncio.get_event_loop()
        future: "asyncio.Future[str]" = loop.create_future()
        _pending_2fa_futures[chat_id] = future
        await bot.send_message(
            chat_id,
            "🔐 Two-Step Verification (2FA) is enabled on this account.\n\n"
            "Please reply with your Telegram cloud password. Your message "
            "will be deleted immediately after it's read.",
        )
        try:
            return await future
        finally:
            _pending_2fa_futures.pop(chat_id, None)

    callbacks = QrLoginCallbacks(on_qr_ready=on_qr_ready, get_2fa_password=get_2fa_password)

    async with session_factory() as session:
        svc = AccountService(session)
        try:
            await svc.perform_qr_login(account_id=account_id, user_id=user_id, callbacks=callbacks)
            await session.commit()
            await bot.send_message(chat_id, "✅ Success! Your account is now connected and ACTIVE.")
        except AuthError as exc:
            await session.rollback()
            await bot.send_message(chat_id, f"❌ {exc}\n\nPlease try /connect again.")
        except Exception as exc:
            await session.rollback()
            log.error("qr_login_task_failed", account_id=account_id, error=str(exc), exc_info=True)
            await bot.send_message(
                chat_id, "❌ Something went wrong connecting your account. Please try /connect again."
            )
        finally:
            _pending_2fa_futures.pop(chat_id, None)


async def maybe_capture_2fa_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Global message handler: resolves a pending 2FA future, if one exists for this chat.

    Registered in an EARLIER handler group than other MessageHandlers (see
    app/bot/bot.py). Raises ApplicationHandlerStop to consume the update
    when it matches so no other handler treats the password as plain text.
    """
    chat = update.effective_chat
    if chat is None or update.message is None:
        return

    future = _pending_2fa_futures.get(chat.id)
    if future is None or future.done():
        return

    password = update.message.text or ""
    try:
        await update.message.delete()
    except Exception:
        pass

    future.set_result(password)
    raise ApplicationHandlerStop


# ---------------------------------------------------------------------------
# /disconnect
# ---------------------------------------------------------------------------

@with_db_and_user
async def disconnect_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    repo = TelegramAccountRepository(session)
    account = await repo.get_by_user_id(user.id)

    if account is None:
        await update.message.reply_text("You have no connected account.")
        return

    keyboard = [
        [InlineKeyboardButton("Yes, disconnect", callback_data=f"disconnect_{account.id}")],
        [InlineKeyboardButton("Cancel", callback_data="disconnect_cancel")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    label = account.username or account.phone_masked or account.id
    await update.message.reply_text(
        f"Are you sure you want to disconnect your account?\n\nAccount: {label}",
        reply_markup=reply_markup,
    )


@with_db_and_user
async def disconnect_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "disconnect_cancel":
        await query.edit_message_text("Disconnection cancelled.")
        return

    if data.startswith("disconnect_"):
        try:
            account_id = int(data.split("_")[1])
        except ValueError:
            return

        svc = AccountService(session)
        try:
            await svc.disconnect_account(account_id=account_id, user_id=user.id)
            await session.commit()
            await query.edit_message_text(
                "✅ Account successfully disconnected. You may /connect a new one."
            )
        except Exception as e:
            await query.edit_message_text(f"❌ Error disconnecting account: {e}")
