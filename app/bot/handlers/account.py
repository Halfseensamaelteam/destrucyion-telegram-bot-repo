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
from telethon.errors import SessionPasswordNeededError

from app.bot.dependencies import with_db_and_user
from app.db.models.user import User
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.account import AccountService, AuthError


# Conversation States
ENTER_PHONE = 1
ENTER_CODE = 2
ENTER_PASSWORD = 3


@with_db_and_user
async def accounts_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /accounts command. Detailed view of connected accounts."""
    repo = TelegramAccountRepository(session)
    accounts = await repo.list_by_user(user.id)

    if not accounts:
        text = "You have no connected accounts. Use /connect to add one."
    else:
        text = "📱 *Your Connected Accounts:*\n\n"
        for acc in accounts:
            text += f"🔹 `{acc.phone_masked}`\n"
            text += f"   Status: {acc.status.value.upper()}\n"
            text += f"   ID: {acc.id}\n\n"
        text += "To remove an account, use /disconnect."

    if update.message:
        await update.message.reply_text(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /connect Conversation Flow
# ---------------------------------------------------------------------------

@with_db_and_user
async def connect_start(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Entry point for /connect."""
    await update.message.reply_text(
        "Let's connect your Telegram account.\n\n"
        "Please enter your phone number in international format (e.g., +1234567890).\n\n"
        "Send /cancel to stop at any time."
    )
    # Store the user ID in context just in case we need it outside the decorator
    context.user_data["user_id"] = user.id
    return ENTER_PHONE


@with_db_and_user
async def connect_phone(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Handle phone number input."""
    phone = update.message.text.strip()
    if not phone.startswith("+"):
        await update.message.reply_text("Phone number must start with '+' (e.g., +1234567890). Try again:")
        return ENTER_PHONE

    msg = await update.message.reply_text(f"Requesting a login code for {phone}...")
    
    svc = AccountService(session)
    try:
        # Create DB record first (DISCONNECTED)
        account = await svc.create_account(user_id=user.id, phone_number=phone)
        await session.commit()
        
        # Request code
        start_result = await svc.start_auth(
            account_id=account.id,
            user_id=user.id,
            phone_number=phone
        )
        await session.commit()
    except Exception as e:
        await msg.edit_text(f"❌ Failed to request code: {e}\n\nPlease try /connect again.")
        return ConversationHandler.END

    # Store state in memory
    context.user_data["phone"] = phone
    context.user_data["account_id"] = account.id
    context.user_data["phone_code_hash"] = start_result.phone_code_hash

    await msg.edit_text(
        f"✅ Code sent to your Telegram app for {phone}.\n\n"
        "Please enter the 5-digit code you received.\n"
        "If you have Two-Step Verification (2FA) enabled, you will be prompted for your password next."
    )
    return ENTER_CODE


@with_db_and_user
async def connect_code(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Handle code input."""
    code = update.message.text.strip()
    
    phone = context.user_data.get("phone")
    account_id = context.user_data.get("account_id")
    phone_code_hash = context.user_data.get("phone_code_hash")

    if not all([phone, account_id, phone_code_hash]):
        await update.message.reply_text("Session expired or corrupted. Please start over with /connect.")
        return ConversationHandler.END

    msg = await update.message.reply_text("Verifying code...")
    svc = AccountService(session)

    try:
        await svc.verify_code(
            account_id=account_id,
            user_id=user.id,
            phone_number=phone,
            code=code,
            phone_code_hash=phone_code_hash,
        )
        await session.commit()
        await msg.edit_text("✅ Success! Your account is now connected and ACTIVE.")
        context.user_data.clear()
        return ConversationHandler.END

    except SessionPasswordNeededError:
        # 2FA required
        await msg.edit_text("🔐 Two-Step Verification (2FA) is enabled.\n\nPlease enter your password:")
        context.user_data["code"] = code # Save code for the next step
        return ENTER_PASSWORD

    except Exception as e:
        await msg.edit_text(f"❌ Verification failed: {e}\n\nPlease try /connect again.")
        context.user_data.clear()
        return ConversationHandler.END


@with_db_and_user
async def connect_password(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> int:
    """Handle 2FA password input."""
    password = update.message.text
    
    phone = context.user_data.get("phone")
    account_id = context.user_data.get("account_id")
    phone_code_hash = context.user_data.get("phone_code_hash")
    code = context.user_data.get("code")

    msg = await update.message.reply_text("Verifying password...")
    
    # Delete the user's message so the password isn't left in the chat history
    try:
        await update.message.delete()
    except Exception:
        pass # Bot might not have delete permission

    svc = AccountService(session)
    try:
        await svc.verify_code(
            account_id=account_id,
            user_id=user.id,
            phone_number=phone,
            code=code,
            phone_code_hash=phone_code_hash,
            password=password,
        )
        await session.commit()
        await msg.edit_text("✅ Success! Your account is now connected and ACTIVE.")
        
    except Exception as e:
        await msg.edit_text(f"❌ Verification failed: {e}\n\nPlease try /connect again.")
        
    context.user_data.clear()
    return ConversationHandler.END


async def connect_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel the /connect flow."""
    context.user_data.clear()
    await update.message.reply_text("Connection cancelled.")
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /disconnect Flow
# ---------------------------------------------------------------------------

@with_db_and_user
async def disconnect_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle /disconnect. Show inline keyboard of accounts to disconnect."""
    repo = TelegramAccountRepository(session)
    accounts = await repo.list_by_user(user.id)

    if not accounts:
        await update.message.reply_text("You have no connected accounts.")
        return

    keyboard = []
    for acc in accounts:
        keyboard.append([InlineKeyboardButton(f"Disconnect {acc.phone_masked}", callback_data=f"disconnect_{acc.id}")])
    
    keyboard.append([InlineKeyboardButton("Cancel", callback_data="disconnect_cancel")])
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text("Select an account to disconnect:", reply_markup=reply_markup)


@with_db_and_user
async def disconnect_callback(
    update: Update, context: ContextTypes.DEFAULT_TYPE, session: AsyncSession, user: User
) -> None:
    """Handle the inline button callback for disconnecting an account."""
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
            # Tenant isolation: disconnect_account validates user ownership
            await svc.disconnect_account(account_id=account_id, user_id=user.id)
            await session.commit()
            await query.edit_message_text(f"✅ Account successfully disconnected.")
        except Exception as e:
            await query.edit_message_text(f"❌ Error disconnecting account: {e}")
