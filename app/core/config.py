"""
app.core.config
~~~~~~~~~~~~~~~
Application configuration loaded from environment variables via pydantic-settings.

All settings are read from the environment (or a .env file if present).
Never hard-code secrets here — use .env.example as the template.
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    admin_telegram_ids: list[int] = Field(
        default_factory=list,
        description="List of integer Telegram user IDs authorized to use /admin commands.",
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
