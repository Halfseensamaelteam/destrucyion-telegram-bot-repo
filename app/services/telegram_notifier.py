"""
app.services.telegram_notifier
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Sends transactional notifications through the Telegram Bot API.
"""

from datetime import timezone, timedelta

from telegram import Bot

from app.core.config import get_settings


class TelegramNotifier:
    """Send bot notifications to Telegram users."""

    def __init__(self) -> None:
        settings = get_settings()
        token = settings.bot_token.get_secret_value()

        if not token:
            raise ValueError("Telegram Bot token is not configured.")

        self._bot = Bot(token=token)

    async def send_subscription_activated(
        self,
        *,
        telegram_user_id: int,
        plan: str,
        expires_at: object | None,
    ) -> None:
        """Notify a user that their subscription has been activated."""

        if expires_at is None:
            expires_line = "Expires: Never (Lifetime access)"
        else:
            wib = timezone(timedelta(hours=7))
            expires_wib = expires_at.astimezone(wib)

            expires_line = (
                f"Expires: "
                f"{expires_wib.strftime('%Y-%m-%d %H:%M WIB')}"
            )

        text = (
            "*Subscription Activated!*\n\n"
            "Pembayaran kamu berhasil dikonfirmasi. "
            "Terima kasih telah berlangganan!\n\n"
            f"*Plan:* {plan.title()}\n"
            "*Payment:* Paid\n"
            f"{expires_line}\n\n"
            "Premium access kamu sekarang sudah aktif.\n"
            "Selamat menikmati semua fitur Premium!"
        )

        await self._bot.send_message(
            chat_id=telegram_user_id,
            text=text,
            parse_mode="Markdown",
        )
