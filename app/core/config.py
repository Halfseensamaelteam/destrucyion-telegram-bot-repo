"""
app.core.config
~~~~~~~~~~~~~~~
Application configuration loaded from environment variables via pydantic-settings.

All settings are read from the environment (or a .env file if present).
Never hard-code secrets here — use .env.example as the template.
"""

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


def _coerce_int_list(v: Any) -> Any:
    """Accept a bare int or a comma-separated string as a list[int].

    Pydantic-settings reads 'ADMIN_TELEGRAM_IDS=12345' as the integer 12345;
    this validator wraps it in a list so the field type stays list[int].
    It also handles a comma-separated string e.g. '12345,67890'.
    """
    if isinstance(v, int):
        return [v]
    if isinstance(v, str):
        v = v.strip().strip("[]")
        return [int(x.strip()) for x in v.split(",") if x.strip()]
    return v


class Settings(BaseSettings):
    """Central application configuration.

    Values are read from environment variables (case-insensitive).
    A .env file in the working directory is loaded automatically.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_env: Literal["development", "staging", "production"] = "development"
    app_secret_key: SecretStr = Field(default=SecretStr("dev-secret-change-me"))

    # ------------------------------------------------------------------
    # Database
    # ------------------------------------------------------------------
    database_url: str = Field(
        default="postgresql+asyncpg://user:password@localhost:5432/destrucyion",
        description="Async-compatible PostgreSQL DSN (asyncpg driver).",
    )

    # ------------------------------------------------------------------
    # Redis
    # ------------------------------------------------------------------
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL.",
    )

    # ------------------------------------------------------------------
    # Telegram MTProto (from https://my.telegram.org)
    # ------------------------------------------------------------------
    telegram_api_id: int = Field(
        default=0,
        description="Telegram App API ID.",
    )
    telegram_api_hash: str = Field(
        default="",
        description="Telegram App API hash.",
    )

    # ------------------------------------------------------------------
    # Telegram Bot (from @BotFather)
    # ------------------------------------------------------------------
    bot_token: SecretStr = Field(
        default=SecretStr(""),
        description="Telegram Bot token from @BotFather.",
    )
    admin_telegram_ids: Annotated[list[int], BeforeValidator(_coerce_int_list)] = Field(
        default_factory=list,
        description="List of integer Telegram user IDs authorized to use /admin commands.",
    )

    # ------------------------------------------------------------------
    # Media capture behaviour (mirrors the original Saveit.py options)
    # ------------------------------------------------------------------
    capture_only_timed: bool = Field(
        default=True,
        description=(
            "If true (default), the worker automatically saves ONLY incoming "
            "timed/self-destructing media (same as Saveit's AUTO_SAVE_TIMED). "
            "If false, every incoming photo/video/document/voice is saved, "
            "which floods Saved Messages in busy chats — not recommended."
        ),
    )
    save_trigger: str = Field(
        default=".saveit",
        description=(
            "Manual save command (Saveit's HANDLER). The account owner "
            "replies to any media message with this text to save it, "
            "regardless of capture_only_timed."
        ),
    )
    display_timezone: str = Field(
        default="UTC",
        description=(
            "IANA timezone name (e.g. 'Asia/Jakarta') used to render "
            "capture timestamps in captions/notifications. Telegram does "
            "not expose a per-user timezone anywhere in its API, so this "
            "is a single operator-configured display timezone, not a "
            "true per-viewer automatic one."
        ),
    )
    admin_notify_chat_id: int | None = Field(
        default=None,
        description=(
            "If set, every captured piece of media also triggers a richer "
            "notification (sender identity + the connected customer's own "
            "details and subscription) sent to this chat/channel via the "
            "Bot API. The bot account must be a member/admin of that chat. "
            "Leave unset to disable admin notifications entirely."
        ),
    )

    # ------------------------------------------------------------------
    # Session encryption
    # ------------------------------------------------------------------
    session_encryption_key: SecretStr = Field(
        default=SecretStr(""),
        description=(
            "32-byte URL-safe base64 Fernet key used to encrypt Telethon "
            "session strings at rest. Generate with: "
            "python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        ),
    )

    # ------------------------------------------------------------------
    # Webhook
    # ------------------------------------------------------------------
    webhook_secret: SecretStr = Field(
        default=SecretStr(""),
        description="Secret token used to validate incoming Telegram Bot webhooks.",
    )

    # ------------------------------------------------------------------
    # Logging
    # ------------------------------------------------------------------
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ------------------------------------------------------------------
    # Optional / integrations
    # ------------------------------------------------------------------
    sentry_dsn: str = Field(
        default="",
        description="Sentry DSN for error tracking. Leave empty to disable.",
    )

    # ------------------------------------------------------------------
    # Midtrans Payment Gateway
    # ------------------------------------------------------------------
    midtrans_client_key: str = Field(
        default="",
        description="Midtrans Client Key.",
    )
    midtrans_server_key: SecretStr = Field(
        default=SecretStr(""),
        description="Midtrans Server Key. Never expose this value.",
    )
    midtrans_is_production: bool = Field(
        default=False,
        description="Use Midtrans production API endpoints. Keep false for Sandbox.",
    )
    midtrans_notification_url: str = Field(
        default="",
        description="Public URL where Midtrans sends payment notifications.",
    )
    subscription_weekly_price: int = Field(
        default=8000,
        description="Weekly subscription price in IDR.",
    )
    subscription_monthly_price: int = Field(
        default=30000,
        description="Monthly subscription price in IDR.",
    )
    subscription_lifetime_price: int = Field(
        default=2000000,
        description="Lifetime subscription price in IDR.",
    )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached application settings singleton.

    Uses lru_cache so the .env file is only parsed once per process.
    Tests can call ``get_settings.cache_clear()`` to force a reload.
    """
    return Settings()
