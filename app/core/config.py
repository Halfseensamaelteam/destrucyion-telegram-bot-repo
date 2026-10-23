"""
app.core.config
~~~~~~~~~~~~~~~
Application configuration loaded from environment variables via pydantic-settings.

All settings are read from the environment (or a .env file if present).
Never hard-code secrets here — use .env.example as the template.
"""

from functools import lru_cache
from typing import Annotated, Any, Literal

from pydantic import BeforeValidator, Field, SecretStr, model_validator
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
    capture_protected_content: bool = Field(
        default=True,
        description=(
            "If true (default), incoming media from chats with 'Restrict "
            "saving content' on (Message.noforwards) is automatically "
            "saved even when capture_only_timed is true — protected posts "
            "are, like timed media, impossible to retrieve later by "
            "forwarding, so download+upload at arrival is the only chance."
        ),
    )
    restricted_trigger: str = Field(
        default=".get",
        description=(
            "Link-save command for the account owner. The owner sends "
            "'<trigger> <t.me link>' in their own Saved Messages and the "
            "worker fetches that post (private/restricted included) with "
            "their own client and saves a copy to Saved Messages. Ranges "
            "('<trigger> https://t.me/c/123/100-110') are supported. "
            "Leave empty to disable the link trigger entirely."
        ),
    )
    restricted_max_batch: int = Field(
        default=20,
        description=(
            "Maximum number of posts one link/range request will fetch. "
            "Caps FloodWait/ban risk from absurd ranges."
        ),
    )
    restricted_batch_delay_seconds: float = Field(
        default=2.0,
        description=(
            "Pause between posts in one range request. FloodWait errors "
            "are additionally retried by SavedMessagesService."
        ),
    )
    download_workers: int = Field(
        default=8,
        description=(
            "Parallel connections used to download a document for the "
            "download+upload resend path. Telethon's plain download_media "
            "uses ONE connection (~1.5 MB/s observed on a VPS); splitting "
            "the file across workers multiplies throughput on fast links. "
            "Set to 1 to disable parallel downloading."
        ),
    )
    download_parallel_min_bytes: int = Field(
        default=2 * 1024 * 1024,
        description=(
            "Files smaller than this skip the parallel downloader — the "
            "connection setup overhead is not worth it for small media."
        ),
    )
    link_request_poll_seconds: float = Field(
        default=3.0,
        description=(
            "How often the worker checks for newly queued link requests. "
            "This is pure waiting time added before a save even starts, "
            "so keep it small; the check is one indexed SELECT."
        ),
    )
    link_request_max_concurrency: int = Field(
        default=10,
        description=(
            "Maximum link requests processed at the same time (one per "
            "account at most). Without a cap, one user's huge video "
            "blocks every other user's saves behind it; with claiming, "
            "several worker processes can also share the queue, each "
            "running up to this many. Acts as the HARD ceiling when the "
            "memory budget below is enabled. Special value 0 = no slot "
            "cap at all ('budget-only' mode): only valid together with "
            "a memory budget — the processor then sizes parallelism "
            "purely from measured RAM (plus an automatic temp-disk "
            "guard). 0 without a budget falls back to the default of 10."
        ),
    )
    link_request_memory_budget_mb: int = Field(
        default=0,
        description=(
            "Adaptive concurrency: when > 0, the worker measures its own "
            "RAM (RSS) before each batch and grows/shrinks how many "
            "requests it claims so total memory stays near this budget "
            "(e.g. 3072 on an 8 GB VPS) instead of a fixed slot count "
            "deciding everything. 0 disables it — the fixed "
            "link_request_max_concurrency applies as before. The budget "
            "covers the whole worker process, clients included."
        ),
    )
    link_request_stale_seconds: float = Field(
        default=600.0,
        description=(
            "A claimed (PROCESSING) link request older than this is "
            "considered abandoned (its worker died) and is requeued. "
            "Must exceed the longest legitimate save duration."
        ),
    )
    link_request_disk_reserve_mb: int = Field(
        default=3072,
        description=(
            "Size-aware disk admission: the worker leases temp-disk "
            "BYTES per link request (its total media size, known from "
            "the fetched messages before any download starts) and only "
            "starts a download while free temp space minus all active "
            "leases stays at or above this reserve. Set 0 to disable "
            "disk admission entirely — behaviour is then exactly the "
            "pre-existing slot-cap/RAM-budget logic."
        ),
    )
    link_request_big_file_mb: int = Field(
        default=100,
        description=(
            "Size-aware disk admission: a link request whose TOTAL "
            "media size exceeds this many MB is 'big class' and leases "
            "against the capped big pool instead of the general disk, "
            "so floods of huge videos cannot crowd out small saves."
        ),
    )
    link_request_big_pool_percent: int = Field(
        default=30,
        ge=0,
        le=100,
        description=(
            "Size-aware disk admission: big-class requests together "
            "may lease at most this percent of the usable temp disk "
            "(free minus the reserve). They keep moving — just bounded "
            "— while the rest of the disk stays available to small "
            "files. A single big file larger than the whole pool is "
            "still admitted on an otherwise idle disk."
        ),
    )
    link_request_big_guarantee_percent: int = Field(
        default=15,
        ge=0,
        le=100,
        description=(
            "Size-aware disk admission: this percent of the usable "
            "temp disk is fenced off from SMALL leases, so the big "
            "class always has room to start even during a small-file "
            "flood (no starvation in either direction)."
        ),
    )
    link_request_class_idle_grace_seconds: float = Field(
        default=30.0,
        ge=0,
        description=(
            "Size-aware disk admission: the pool/guarantee split is "
            "work-conserving — a class's protection only binds while "
            "that class is PRESENT (holding leases, waiting for one, "
            "or granted one within this grace window, which covers "
            "the gaps between claim batches). While a class is "
            "absent, the other class may borrow the whole usable "
            "disk instead of idling next to free space."
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
    # Public web URL
    # ------------------------------------------------------------------
    web_base_url: str = Field(
        default="http://localhost:8000",
        description="Public HTTPS base URL used for one-time browser login links.",
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
    # Payment method availability
    # ------------------------------------------------------------------
    enable_midtrans_core_payments: bool = Field(
        default=True,
        description="Show Indonesia Payments V1 (Midtrans Core API: QRIS and bank transfers).",
    )
    enable_midtrans_payments_link: bool = Field(
        default=True,
        description="Show Indonesia Payments V2 (Midtrans hosted Payment Link checkout).",
    )
    enable_nowpayments: bool = Field(
        default=True,
        description="Show NOWPayments crypto as a subscription payment method in the Telegram bot.",
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
    # ------------------------------------------------------------------
    # NOWPayments
    # ------------------------------------------------------------------
    nowpayments_environment: Literal["sandbox", "production"] = Field(
        default="production",
        description="NOWPayments API environment. Sandbox and production credentials are kept separate.",
    )
    nowpayments_sandbox_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="NOWPayments sandbox API key. Used only when NOWPAYMENTS_ENVIRONMENT=sandbox.",
    )
    nowpayments_sandbox_ipn_secret: SecretStr = Field(
        default=SecretStr(""),
        description="NOWPayments sandbox IPN secret. Used only when NOWPAYMENTS_ENVIRONMENT=sandbox.",
    )
    nowpayments_sandbox_base_url: str = Field(
        default="https://api-sandbox.nowpayments.io/v1",
        description="Official NOWPayments sandbox API base URL.",
    )
    nowpayments_api_key: SecretStr = Field(
        default=SecretStr(""),
        description="NOWPayments API key. Never expose this value.",
    )
    nowpayments_ipn_secret: SecretStr = Field(
        default=SecretStr(""),
        description="NOWPayments IPN Secret Key used to verify webhook signatures.",
    )
    nowpayments_ipn_callback_url: str = Field(
        default="",
        description="Public HTTPS URL for the NOWPayments IPN callback endpoint.",
    )
    nowpayments_payout_addresses: str = Field(
        default="",
        description=(
            "Comma-separated NOWPayments payout destinations as code=address pairs, "
            "for example usdcbase=0x... . Never expose or commit private keys."
        ),
    )
    nowpayments_pay_currency: str = Field(
        default="usdttrc20",
        description="NOWPayments pay_currency code; intended initial network is USDT TRC20.",
    )
    nowpayments_base_url: str = Field(
        default="https://api.nowpayments.io/v1",
        description="NOWPayments API base URL.",
    )

    subscription_weekly_price: int = Field(
        default=8000,
        description="Weekly subscription price in IDR.",
    )
    subscription_monthly_price: int = Field(
        default=30000,
        description="Monthly subscription price in IDR.",
    )
    subscription_3_monthly_price: int = Field(
        default=90000,
        description="Three-month subscription price in IDR.",
    )
    subscription_lifetime_price: int = Field(
        default=2000000,
        description="Lifetime subscription price in IDR.",
    )

    # ------------------------------------------------------------------
    # Auto-Capture add-on (/capture) — paid add-on prices in IDR.
    # The add-on is sold ONLY inside /capture (never in /subscription)
    # and uses the same payment machinery with CAP- order ids.
    # ------------------------------------------------------------------
    capture_weekly_price: int = Field(
        default=15000,
        description="Auto-Capture add-on weekly (7 days) price in IDR.",
    )
    capture_monthly_price: int = Field(
        default=55000,
        description="Auto-Capture add-on monthly (30 days) price in IDR.",
    )
    capture_3_monthly_price: int = Field(
        default=165000,
        description="Auto-Capture add-on three-month (90 days) price in IDR.",
    )

    # ------------------------------------------------------------------
    # Derived helpers
    # ------------------------------------------------------------------
    @property
    def is_development(self) -> bool:
        return self.app_env == "development"

    @model_validator(mode="after")
    def validate_phone_login_url(self) -> "Settings":
        """Require an absolute HTTPS URL for browser login outside development."""
        from urllib.parse import urlparse

        parsed = urlparse(self.web_base_url)
        if not parsed.netloc:
            raise ValueError("web_base_url must be an absolute URL")
        if self.app_env != "development" and parsed.scheme != "https":
            raise ValueError("web_base_url must use HTTPS outside development")
        return self

    @property
    def nowpayments_active_api_key(self) -> SecretStr:
        """Return credentials only for the explicitly selected NOWPayments environment."""
        if self.nowpayments_environment == "sandbox":
            return self.nowpayments_sandbox_api_key
        return self.nowpayments_api_key

    @property
    def nowpayments_active_ipn_secret(self) -> SecretStr:
        """Return the IPN secret matching the selected NOWPayments environment."""
        if self.nowpayments_environment == "sandbox":
            return self.nowpayments_sandbox_ipn_secret
        return self.nowpayments_ipn_secret

    @property
    def nowpayments_active_base_url(self) -> str:
        """Return the API base URL matching the selected NOWPayments environment."""
        if self.nowpayments_environment == "sandbox":
            return self.nowpayments_sandbox_base_url
        return self.nowpayments_base_url

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