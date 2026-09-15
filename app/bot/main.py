"""
app.bot.main
~~~~~~~~~~~~
Entry point to run the Telegram Bot via long polling.

Run locally:
    python -m app.bot.main
"""

from app.bot.bot import create_bot_app
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger(__name__)


def main() -> None:
    log.info("bot_starting", env=settings.app_env)
    application = create_bot_app()
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()