"""
app.main
~~~~~~~~
FastAPI application entry point.

This module creates and configures the FastAPI app instance.

Run locally:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import (
    check_redis_health,
    get_redis_client,
    reset_redis_client,
)
from app.db.session import _get_engine, check_db_health

# Phase 18: Bot webhook integration
_bot_app = None

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger(__name__)


# Initialize rate limiter
# Uses in-memory storage in development/test mode.
_limiter: Limiter | None = None


def get_limiter() -> Limiter:
    """Get or create the rate limiter instance."""
    global _limiter

    if _limiter is None:
        # Use in-memory storage in development/test mode
        # or when Redis is not configured.
        if settings.is_development or not settings.redis_url:
            storage_uri = "memory://"

            log.info(
                "rate_limiter_memory_mode",
                msg="Using in-memory rate limiting (dev/test mode)",
            )

        else:
            # Check Redis availability before using it
            storage_uri = "memory://"

            try:
                import redis as sync_redis

                probe = sync_redis.Redis.from_url(
                    settings.redis_url,
                    socket_connect_timeout=2,
                    socket_timeout=2,
                )

                try:
                    probe.ping()
                    storage_uri = settings.redis_url
                finally:
                    probe.close()

            except Exception as exc:
                log.warning(
                    "rate_limiter_memory_fallback",
                    error=str(exc),
                    msg="Redis unreachable at startup, using in-memory rate limiting",
                )

        _limiter = Limiter(
            key_func=get_remote_address,
            storage_uri=storage_uri,
        )

    return _limiter


def override_limiter(limiter: Limiter) -> None:
    """Override the rate limiter for testing."""
    global _limiter
    _limiter = limiter


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Graceful shutdown lifecycle events."""
    yield

    # Cleanup DB connection pool
    engine = _get_engine()
    await engine.dispose()

    # Cleanup Redis connection
    reset_redis_client()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""

    application = FastAPI(
        title="Destrucyion Telegram Bot",
        description=(
            "Multi-tenant Telegram media preservation service. "
            "Preserves timed/self-destructing Telegram media to Saved Messages."
        ),
        version="0.1.0",
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        lifespan=lifespan,
    )

    limiter = get_limiter()

    application.state.limiter = limiter

    application.add_exception_handler(
        RateLimitExceeded,
        _rate_limit_exceeded_handler,
    )

    # ---------------------------------------------------------
    # Root endpoint
    # ---------------------------------------------------------

    @application.api_route(
        "/",
        methods=["GET", "HEAD"],
        tags=["health"],
    )
    async def root() -> JSONResponse:
        """Simple root endpoint for Render health checks."""
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "service": "destrucyion-telegram-bot",
                "version": "0.1.0",
            },
        )

    # ---------------------------------------------------------
    # Health endpoint
    # ---------------------------------------------------------

    @application.get(
        "/api/health",
        tags=["health"],
    )
    async def health(
        db_healthy: bool = Depends(check_db_health),
        redis_healthy: bool = Depends(check_redis_health),
    ) -> JSONResponse:
        """Return application health status."""

        status = "ok" if db_healthy and redis_healthy else "error"

        if status == "error":
            log.error(
                "health_check_failed",
                db_healthy=db_healthy,
                redis_healthy=redis_healthy,
            )

        return JSONResponse(
            status_code=200 if status == "ok" else 503,
            content={
                "status": status,
                "service": "destrucyion-telegram-bot",
                "version": "0.1.0",
                "environment": settings.app_env,
                "checks": {
                    "database": "ok" if db_healthy else "error",
                    "redis": "ok" if redis_healthy else "error",
                },
            },
        )

    # ---------------------------------------------------------
    # Telegram Bot Webhook
    # ---------------------------------------------------------

    @application.post(
        "/webhook/telegram",
        tags=["bot"],
    )
    async def telegram_webhook(request: Request) -> JSONResponse:
        """Receive Telegram Bot updates via webhook."""

        global _bot_app

        # Lazy load bot application
        if _bot_app is None:
            from app.bot.bot import create_bot_app

            _bot_app = create_bot_app()

            await _bot_app.initialize()

            log.info("bot_webhook_loaded")

        # Read request body
        import json

        try:
            body = await request.body()
            update_data = json.loads(body.decode())

        except json.JSONDecodeError:
            log.warning("bot_webhook_invalid_json")

            return JSONResponse(
                status_code=400,
                content={
                    "status": "error",
                    "message": "Invalid JSON",
                },
            )

        # Validate Telegram update
        if not update_data or "update_id" not in update_data:
            log.warning(
                "bot_webhook_invalid_update",
                update_data=update_data,
            )

            return JSONResponse(
                status_code=200,
                content={"status": "ok"},
            )

        # Process Telegram update
        try:
            from telegram import Update

            update = Update.de_json(
                update_data,
                _bot_app.bot,
            )

            await _bot_app.process_update(update)

        except Exception as exc:
            log.error(
                "bot_webhook_processing_error",
                error=str(exc),
            )

            # Always return 200 to prevent Telegram retries
            pass

        return JSONResponse(
            status_code=200,
            content={"status": "ok"},
        )

    # ---------------------------------------------------------
    # REST API routers
    # ---------------------------------------------------------

    from app.api.routes import (
        admin,
        media,
        midtrans,
        phone_login,
        subscriptions,
        telegram,
        users,
    )

    application.include_router(users.router)
    application.include_router(telegram.router)
    application.include_router(subscriptions.router)
    application.include_router(media.router)
    application.include_router(admin.router)
    application.include_router(midtrans.router)

    # ---------------------------------------------------------
    # Phone Login
    # ---------------------------------------------------------
    #
    # Secure browser-based Telegram phone login.
    #
    # The login token is one-time and short-lived.
    # OTP / 2FA are entered directly in the browser.
    #
    application.include_router(phone_login.router)

    log.info(
        "app_created",
        env=settings.app_env,
    )

    return application


app = create_app()