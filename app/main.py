"""
app.main
~~~~~~~~
FastAPI application entry point.

This module creates and configures the FastAPI app instance.
In Phase 1, the app runs with a single /health endpoint as a skeleton.
Business routes, database, and bot webhook are wired in later phases.

Run locally:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.redis import check_redis_health, get_redis_client
from app.db.session import _get_engine, check_db_health

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger(__name__)


# Initialize rate limiter (using Redis if available, fallback to memory)
redis_client = get_redis_client()
storage_uri = settings.redis_url if redis_client else "memory://"
limiter = Limiter(key_func=get_remote_address, storage_uri=storage_uri)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Graceful shutdown lifecycle events.
    
    Phase 17: Enhanced cleanup with proper connection closing.
    """
    yield
    # Cleanup DB connection pool
    engine = _get_engine()
    await engine.dispose()
    # Cleanup Redis connection
    from app.core.redis import reset_redis_client
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

    application.state.limiter = limiter
    application.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Health check (public)
    @application.get("/api/health", tags=["health"])
    async def health() -> JSONResponse:
        """Return application health status, verifying DB and Redis.
        
        Phase 17: Uses dedicated health check functions with connection recovery.
        """
        db_healthy = await check_db_health()
        redis_healthy = await check_redis_health()
        
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
            }
        )

    # Phase 12: REST API routers
    from app.api.routes import users, telegram, subscriptions, media, admin

    application.include_router(users.router)
    application.include_router(telegram.router)
    application.include_router(subscriptions.router)
    application.include_router(media.router)
    application.include_router(admin.router)

    log.info("app_created", env=settings.app_env)
    return application


app = create_app()

