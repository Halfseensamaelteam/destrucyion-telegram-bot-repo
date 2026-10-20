"""app.bot.bot
~~~~~~~~~~~
Telegram Bot application factory using python-telegram-bot.
"""

from telegram import Update
from telegram.ext import (
    TypeHandler,
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
from app.bot.handlers.save import save_command
from app.bot.handlers.account import (
    accounts_command,
    connect_start,
    connect_api_id,
    connect_api_hash,
    connect_cancel,
    disconnect_command,
    disconnect_callback,
    maybe_capture_2fa_password,
    CONNECT_METHOD,
    ENTER_API_ID,
    ENTER_API_HASH,
    connect_qr_choice,
)
from app.bot.handlers.subscription import subscription_command
from app.bot.handlers.subscription_payment import subscription_payment_callback
from app.bot.handlers.admin import admin_command
from app.bot.handlers.phone_login import connect_phone_command
from app.bot.handlers.channel_gate import channel_membership_gate, check_channel_membership


def create_bot_app() -> Application:
    """Create and configure the python-telegram-bot Application."""
    settings = get_settings()
    token = settings.bot_token.get_secret_value()

    if not token:
        token = "123456789:dummy-token-for-tests"

    import os
    os.makedirs("data", exist_ok=True)
    persistence = PicklePersistence(filepath="data/bot_persistence.pickle")
    
    application = (
        Application.builder()
        .token(token)
        .persistence(persistence)
        .build()
    )

    application.add_handler(CallbackQueryHandler(check_channel_membership, pattern="^check_channel_membership$"), group=-3)
    application.add_handler(TypeHandler(Update, channel_membership_gate), group=-2)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("save", save_command))

    application.add_handler(CommandHandler("subscription", subscription_command))
    application.add_handler(
        CallbackQueryHandler(
            subscription_payment_callback,
            pattern="^(subscription_qris_menu$|subscription_payment_link_menu$|subscription_link_plan_|subscription_buy_|subscription_add_plan$|subscription_bank_|subscription_indonesia_pay:|nowpayments_menu$|nowpayments_plan_|nowpayments_pay_|nowpayments_asset_|nowpayments_target_|np:)",
        )
    )

    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("disconnect", disconnect_command))
    application.add_handler(CommandHandler("connectphone", connect_phone_command))
    application.add_handler(CallbackQueryHandler(disconnect_callback, pattern="^disconnect_"))

    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, maybe_capture_2fa_password),
        group=-1,
    )

    connect_handler = ConversationHandler(
        entry_points=[CommandHandler("connect", connect_start)],
        states={
            CONNECT_METHOD: [
                CallbackQueryHandler(connect_phone_command, pattern="^connect_phone$"),
                CallbackQueryHandler(connect_qr_choice, pattern="^connect_qr$"),
            ],
            ENTER_API_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_api_id)
            ],
            ENTER_API_HASH: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, connect_api_hash)
            ],
        },
        fallbacks=[CommandHandler("cancel", connect_cancel)],
        name="connect_conversation",
        persistent=True,
        allow_reentry=True,
        conversation_timeout=600,
    )
    application.add_handler(connect_handler)

    return application


async def setup_webhook(application: Application, webhook_url: str) -> None:
    """Set up the Telegram Bot webhook."""
    await application.bot.set_webhook(url=webhook_url)
    print(f"Webhook set to: {webhook_url}")


async def delete_webhook(application: Application) -> None:
    """Delete webhook when switching to long polling."""
    await application.bot.delete_webhook()
    print("Webhook deleted")
