"""
app.bot.handlers.account
~~~~~~~~~~~~~~~~~~~~~~~~
Account management commands: /accounts, /connect, /disconnect.

Implements the multi-step Telethon authentication flow using a ConversationHandler.
State is stored in PTB's in-memory `user_data` dict.
"""

from sqlalchemy.ext.asyncio import AsyncSession
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

from app.bot.dependencies import with_db_and_user, admin_required
from app.db.models.user import User
from app.db.models.telegram_account import TelegramAccountStatus
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.account import AccountService, AuthError


# Conversation States
ENTER_API_ID = 1
ENTER_API_HASH = 2


@with_db_and_user
@admin_required
async def accounts_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /accounts command. Shows the single bound account status."""
    repo = TelegramAccountRepository(session)
    accounts = await repo.list_by_user(user.id)

    if not accounts:
        text = "You have no connected account. Use /connect to add one."
    else:
        # With 1-User-to-1-Account constraint, there should be at most 1 account
        acc = accounts[0]
        text = "📱 *Your Connected Account:*\n\n"
        if acc.username:
            text += f"🔹 @{acc.username}\n"
        else:
            text += f"🔹 Account ID: {acc.telegram_user_id}\n"
        text += f"   Status: {acc.status.value.upper()}\n"
        text += f"   API ID: {acc.api_id if acc.api_id else 'Not set'}\n\n"
        text += "To disconnect, use /disconnect."

    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /connect Conversation Flow (QR Code Authentication)
# ---------------------------------------------------------------------------

@with_db_and_user
@admin_required
async def connect_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Entry point for /connect using QR code authentication."""
    # Check if user already has an active account
    repo = TelegramAccountRepository(session)
    existing_accounts = await repo.list_by_user(user.id)
    for acc in existing_accounts:
        if acc.status == TelegramAccountStatus.ACTIVE:
            await update.message.reply_text(
                "You already have an active account. Please disconnect it first using /disconnect."
            )
            return ConversationHandler.END

    await update.message.reply_text(
        "Let's connect your Telegram account using QR code.\n\n"
        "First, I need your Telegram API credentials.\n\n"
        "Please enter your API ID (from https://my.telegram.org/apps).\n\n"
        "Send /cancel to stop at any time."
    )
    context.user_data["user_id"] = user.id
    return ENTER_API_ID


@with_db_and_user
@admin_required
async def connect_api_id(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Handle API ID input."""
    api_id_text = update.message.text.strip()
    
    try:
        api_id = int(api_id_text)
    except ValueError:
        await update.message.reply_text("API ID must be a number. Please try again:")
        return ENTER_API_ID

    context.user_data["api_id"] = api_id
    await update.message.reply_text(
        "✅ API ID received.\n\n"
        "Now please enter your API Hash (from https://my.telegram.org/apps):"
    )
    return ENTER_API_HASH


@with_db_and_user
@admin_required
async def connect_api_hash(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Handle API Hash input and generate QR code."""
    api_hash = update.message.text.strip()
    api_id = context.user_data.get("api_id")

    if not api_id:
        await update.message.reply_text("Session expired. Please start over with /connect.")
        return ConversationHandler.END

    msg = await update.message.reply_text("Generating QR code...")
    
    svc = AccountService(session)
    try:
        # Create DB record first (DISCONNECTED)
        account = await svc.create_account(user_id=user.id, phone_number="QR_AUTH")
        await session.commit()
        
        # Generate QR code
        qr_result = await svc.start_qr_auth(
            account_id=account.id,
            user_id=user.id,
            api_id=api_id,
            api_hash=api_hash,
        )
        
        # Store state for waiting phase
        context.user_data["account_id"] = account.id
        context.user_data["api_id"] = api_id
        context.user_data["api_hash"] = api_hash
        context.user_data["login_obj"] = qr_result.login_obj

        # Send QR code image
        from io import BytesIO
        await msg.delete()
        await update.message.reply_photo(
            photo=BytesIO(qr_result.qr_code_bytes),
            caption=(
                "📱 Scan this QR code with your Telegram app to connect.\n\n"
                "Instructions:\n"
                "1. Open Telegram on your phone\n"
                "2. Go to Settings > Devices > Link Desktop Device\n"
                "3. Scan this QR code\n\n"
                "Waiting for scan... (timeout: 5 minutes)"
            ),
        )

        # Start waiting for QR scan in background
        import asyncio
        asyncio.create_task(_wait_for_qr_scan(update, context, session, user))
        
        return ConversationHandler.END

    except AuthError as e:
        await msg.edit_text(f"❌ Error: {e}\n\nPlease try /connect again.")
        return ConversationHandler.END
    except Exception as e:
        await msg.edit_text(f"❌ Failed to generate QR code: {e}\n\nPlease try /connect again.")
        return ConversationHandler.END


async def _wait_for_qr_scan(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Background task to wait for QR code scan completion."""
    account_id = context.user_data.get("account_id")
    api_id = context.user_data.get("api_id")
    api_hash = context.user_data.get("api_hash")
    login_obj = context.user_data.get("login_obj")

    if not all([account_id, api_id, api_hash, login_obj]):
        return

    svc = AccountService(session)
    try:
        await svc.wait_qr_auth(
            account_id=account_id,
            user_id=user.id,
            login_obj=login_obj,
            api_id=api_id,
            api_hash=api_hash,
        )
        await session.commit()
        
        # Notify user of success
        if update.effective_message:
            await update.effective_message.reply_text(
                "✅ Success! Your account is now connected and ACTIVE."
            )
    except AuthError as e:
        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Authentication failed: {e}")
    except Exception as e:
        if update.effective_message:
            await update.effective_message.reply_text(f"❌ Error: {e}")
    finally:
        context.user_data.clear()


async def connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the /connect flow."""
    context.user_data.clear()
    await update.message.reply_text("Connection cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /disconnect Flow
# ---------------------------------------------------------------------------

@with_db_and_user
@admin_required
async def disconnect_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /disconnect. Disconnect the user's single account."""
    repo = TelegramAccountRepository(session)
    accounts = await repo.list_by_user(user.id)

    if not accounts:
        await update.message.reply_text("You have no connected account.")
        return

    # With 1-User-to-1-Account, disconnect the first (and only) account
    account = accounts[0]
    
    keyboard = [
        [InlineKeyboardButton("Yes, disconnect", callback_data="disconnect_confirm")],
        [InlineKeyboardButton("Cancel", callback_data="disconnect_cancel")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        f"Are you sure you want to disconnect your account?\n\n"
        f"Account: {account.username if account.username else account.phone_masked}",
        reply_markup=reply_markup
    )


@with_db_and_user
@admin_required
async def disconnect_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle the inline button callback for disconnecting."""
    query = update.callback_query
    await query.answer()

    data = query.data
    if data == "disconnect_cancel":
        await query.edit_message_text("Disconnection cancelled.")
        return

    if data == "disconnect_confirm":
        repo = TelegramAccountRepository(session)
        accounts = await repo.list_by_user(user.id)
        
        if not accounts:
            await query.edit_message_text("No account to disconnect.")
            return

        account = accounts[0]
        svc = AccountService(session)
        try:
            await svc.disconnect_account(account_id=account.id, user_id=user.id)
            await session.commit()
            await query.edit_message_text("✅ Account successfully disconnected.")
        except Exception as e:
            await query.edit_message_text(f"❌ Error disconnecting account: {e}")
