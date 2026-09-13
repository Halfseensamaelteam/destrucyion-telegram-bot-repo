"""
app.bot.bot
~~~~~~~~~~~
Telegram Bot application factory using python-telegram-bot.
"""

from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    PicklePersistence,
)

from app.core.config import get_settings
from app.bot.handlers.start import start_command, help_command, status_command
from app.bot.handlers.account import (
    accounts_command,
    connect_start,
    connect_phone,
    connect_code,
    connect_password,
    connect_cancel,
    disconnect_command,
    disconnect_callback,
    ENTER_PHONE,
    ENTER_CODE,
    ENTER_PASSWORD,
)
from app.bot.handlers.subscription import subscription_command
from app.bot.handlers.admin import admin_command


def create_bot_app() -> Application:
    """Create and configure the python-telegram-bot Application."""
    settings = get_settings()
    token = settings.bot_token.get_secret_value()

    if not token:
        # Prevent crash during tests or when bot token is not yet configured.
        # It won't be able to run, but it can be constructed.
        token = "123456789:dummy-token-for-tests"

    import os
    os.makedirs("data", exist_ok=True)
    # Use PicklePersistence so conversation states survive bot restarts
    persistence = PicklePersistence(filepath="data/bot_persistence.pickle")
    
    application = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .build()
    )

    # Basic Commands
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    
    # Subscription
    application.add_handler(CommandHandler("subscription", subscription_command))

    # Admin
    application.add_handler(CommandHandler("admin", admin_command))

    # Accounts / Disconnect
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("disconnect", disconnect_command))
    application.add_handler(CallbackQueryHandler(disconnect_callback, pattern="^disconnect_"))

    # Connect Conversation Flow
    connect_handler = ConversationHandler(
        entry_points=[CommandHandler("connect", connect_start)],
        states={
            ENTER_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_phone)
            ],
            ENTER_CODE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_code)
            ],
            ENTER_PASSWORD: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_password)
            ],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel)],
        name="connect_conversation",
        persistent=True,
    )
    application.add_handler(connect_handler)

    return application
