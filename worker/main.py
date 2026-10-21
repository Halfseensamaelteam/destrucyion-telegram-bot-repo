"""
worker.main
~~~~~~~~~~~
Persistent worker entry point.

The worker owns long-lived Telethon clients and consumes manual-save jobs.
It MUST NOT run on Vercel or any serverless platform.

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
    """Start managed Telegram clients and the Redis manual-save consumer."""
    log.info(
        "worker_starting",
        env=settings.app_env,
        phase="10-idempotency-recovery",
    )

    from app.db.session import _get_session_factory
    from app.telegram.events import make_handler_factory
    from app.services.recovery import RecoveryService
    from worker.save_jobs import consume_save_jobs
    from worker.supervisor import TelegramClientManager

    session_factory = _get_session_factory()
    handler_factory = make_handler_factory(session_factory)
    manager = TelegramClientManager(session_factory, handler_factory=handler_factory)

    import signal
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(manager.stop()))
        except NotImplementedError:
            pass  # Windows doesn't fully support add_signal_handler.

    await manager.start()
    log.info("worker_started", status="manager running")

    recovery = RecoveryService(session_factory, manager)
    recovery_summary = await recovery.run()
    log.info("worker_recovery_done", **recovery_summary)

    save_job_task = asyncio.create_task(
        consume_save_jobs(manager, session_factory),
        name="manual-save-job-consumer",
    )
    try:
        await manager.run_forever()
    except asyncio.CancelledError:
        pass
    finally:
        save_job_task.cancel()
        try:
            await save_job_task
        except asyncio.CancelledError:
            pass
        await manager.stop()
        log.info("worker_stopped")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
