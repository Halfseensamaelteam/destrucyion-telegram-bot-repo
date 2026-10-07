"""Telegram command that starts the secure browser phone-login flow."""
from telegram import Update
from telegram.ext import ContextTypes

from app.bot.dependencies import with_db_and_user
from app.core.config import get_settings
from app.db.models.user import User
from app.db.session import _get_session_factory
from app.db.repositories.telegram_account_repo import TelegramAccountRepository
from app.services.phone_login import PhoneLoginCoordinator

coordinator = PhoneLoginCoordinator()


@with_db_and_user
async def connect_phone_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    session,
    user: User,
) -> None:
    """Issue a one-time browser link for Telegram phone authentication."""
    repo = TelegramAccountRepository(session)
    account = await repo.get_by_user_id(user.id)
    if account is not None and account.status.value != "disconnected":
        message = update.effective_message
    if message is None:
        return ConversationHandler.END

    await message.reply_text(
            "You already have a connected account (or a connection is in progress). "
            "Use /disconnect first."
        )
        return

    token = await coordinator.issue(
        user_id=user.id,
        telegram_user_id=user.telegram_user_id,
        account_id=None,
    )
    settings = get_settings()
    try:
        settings.validate_phone_login_url()
    except ValueError as exc:
        await update.message.reply_text(
            "Phone login is temporarily unavailable because the web login URL is not configured securely."
        )
        raise RuntimeError("Invalid phone-login web_base_url configuration") from exc
    base = settings.web_base_url.rstrip("/")
    url = f"{base}/api/v1/phone-login/start#token={token}"
    await update.message.reply_text(
        "📱 Open this secure one-time link in your browser:\n\n"
        f"{url}\n\n"
        "It expires in 10 minutes and can only be used once. "
        "Do not share it. Your Telegram login code and optional 2FA password "
        "must be entered only on the HTTPS page."
    )
