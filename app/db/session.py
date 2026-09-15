"""
app.db.session
~~~~~~~~~~~~~~
Async SQLAlchemy engine and session factory.

Supports both PostgreSQL (asyncpg) and SQLite (aiosqlite) via DATABASE_URL.
The correct driver is selected automatically from the URL scheme:
    postgresql+asyncpg://...
    sqlite+aiosqlite:///...

Phase 17: Production Hardening - Added connection recovery and health check.
"""

import asyncio
from collections.abc import AsyncGenerator

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, DisconnectionError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings
from app.core.logging import get_logger

log = get_logger(__name__)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None
# Reconnect lock to prevent multiple simultaneous reconnect attempts
_reconnect_lock = asyncio.Lock()
# Maximum reconnect attempts before giving up
_MAX_RECONNECT_ATTEMPTS = 3
# Delay between reconnect attempts (seconds)
_RECONNECT_DELAY = 2.0


def _get_engine() -> AsyncEngine:
    """Return the singleton async engine, creating it on first call."""
    global _engine
    if _engine is None:
        settings = get_settings()
        connect_args: dict = {}
        if settings.database_url.startswith("sqlite"):
            # SQLite requires check_same_thread=False for async use
            connect_args["check_same_thread"] = False
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.is_development,
            pool_pre_ping=True,  # Phase 17: Check connection health before use
            pool_recycle=3600,  # Phase 17: Recycle connections after 1 hour
            connect_args=connect_args,
        )
    return _engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the singleton async session factory."""
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=_get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
            autoflush=False,
        )
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency — yields one AsyncSession per request.

    Usage::

        async def my_endpoint(session: AsyncSession = Depends(get_session)):
            ...
    """
    factory = _get_session_factory()
    async with factory() as session:
        yield session


async def create_all_tables() -> None:
    """Create all tables (development/test helper — use Alembic in production)."""
    from app.db.models import Base  # noqa: PLC0415 — avoid circular import at module level

    async with _get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def drop_all_tables() -> None:
    """Drop all tables (test helper only — never use in production)."""
    from app.db.models import Base  # noqa: PLC0415

    async with _get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def override_engine(engine: AsyncEngine) -> None:
    """Replace the engine with a test engine. Call before any session usage."""
    global _engine, _session_factory
    _engine = engine
    _session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


# ----------------------------------------------------------------------
# Phase 17: Database Connection Recovery
# ----------------------------------------------------------------------


async def check_db_health() -> bool:
    """Check if the database connection is healthy.

    Returns:
        True if the database is reachable, False otherwise.
    """
    engine = _get_engine()
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except (DBAPIError, DisconnectionError, Exception) as exc:
        log.warning("db_health_check_failed", error=str(exc))
        return False


async def reconnect_database() -> bool:
    """Attempt to reconnect to the database.

    This function disposes the current engine and creates a new one.
    It uses a lock to prevent multiple simultaneous reconnect attempts.

    Returns:
        True if reconnection succeeded, False otherwise.
    """
    global _engine, _session_factory

    async with _reconnect_lock:
        # Check if already reconnected by another caller
        if await check_db_health():
            log.info("db_reconnect_already_healthy")
            return True

        log.warning("db_reconnect_attempting")
        
        for attempt in range(_MAX_RECONNECT_ATTEMPTS):
            try:
                # Dispose old engine
                if _engine is not None:
                    await _engine.dispose()
                
                # Reset globals to force recreation
                _engine = None
                _session_factory = None
                
                # Create new engine
                new_engine = _get_engine()
                
                # Test the new connection
                async with new_engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                
                log.info("db_reconnect_success", attempt=attempt + 1)
                return True
                
            except Exception as exc:
                log.warning(
                    "db_reconnect_failed",
                    attempt=attempt + 1,
                    max_attempts=_MAX_RECONNECT_ATTEMPTS,
                    error=str(exc),
                )
                if attempt < _MAX_RECONNECT_ATTEMPTS - 1:
                    await asyncio.sleep(_RECONNECT_DELAY)
        
        log.error("db_reconnect_gave_up", max_attempts=_MAX_RECONNECT_ATTEMPTS)
        return False


async def get_session_with_retry() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields a session with automatic retry on DB failure.

    If the database is unreachable, this will attempt to reconnect before
    yielding the session. If reconnection fails, it will raise an exception.

    Usage::

        async def my_endpoint(session: AsyncSession = Depends(get_session_with_retry)):
            ...
    """
    # Check health first
    if not await check_db_health():
        log.warning("db_unhealthy_attempting_reconnect")
        if not await reconnect_database():
            raise RuntimeError("Database is unreachable after reconnection attempts")
    
    factory = _get_session_factory()
    async with factory() as session:
        yield session
