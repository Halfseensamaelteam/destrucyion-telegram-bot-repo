"""
worker.main
~~~~~~~~~~~
Persistent worker entry point.

The worker is responsible for running long-lived Telethon clients.
It MUST NOT run on Vercel or any serverless platform — it requires a
persistent process (VPS, Docker host, Railway, Render, Fly.io, etc.).

In Phase 1, the worker starts and logs a startup message only.
The TelegramClientManager and account loading are implemented in Phase 6.

Run locally:
    python -m worker.main
"""

import asyncio

from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.log_level)
log = get_logger(__name__)


async def run() -> None:
    """Main worker coroutine.

    Phase 1: skeleton only — logs startup and exits.
    Phase 6+: loads accounts, starts Telethon clients, processes media.
    """
    log.info(
        "worker_starting",
        env=settings.app_env,
        phase="10-idempotency-recovery",
    )
    
    from app.db.session import _get_session_factory
    from app.telegram.events import make_handler_factory
    from app.services.recovery import RecoveryService
    from worker.supervisor import TelegramClientManager
    
    session_factory = _get_session_factory()
    handler_factory = make_handler_factory(session_factory)
    manager = TelegramClientManager(session_factory, handler_factory=handler_factory)
    
    # Catch SIGINT and SIGTERM to gracefully stop the manager
    import signal
    loop = asyncio.get_running_loop()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(manager.stop()))
        except NotImplementedError:
            pass # Windows doesn't fully support add_signal_handler
            
    await manager.start()
    log.info("worker_started", status="manager running")

    # Phase 10: Recovery scan — retry any PENDING records from before the restart.
    # Runs once synchronously before entering the event loop.
    recovery = RecoveryService(session_factory, manager)
    recovery_summary = await recovery.run()
    log.info("worker_recovery_done", **recovery_summary)
    
    try:
        await manager.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        await manager.stop()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
