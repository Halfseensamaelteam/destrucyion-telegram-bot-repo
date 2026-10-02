#!/bin/bash
set -e

SERVICE_ROLE="${SERVICE_ROLE:-web}"

run_migrations() {
    echo "==> Running database migrations..."
    alembic upgrade head
}

start_bot() {
    echo "==> Starting Telegram Bot..."
    python -m app.bot.main &
}

start_worker() {
    echo "==> Starting Worker..."
    python -m worker.main &
}

start_web() {
    echo "==> Starting FastAPI Web Service on port ${PORT:-8000}..."
    exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
}

case "$SERVICE_ROLE" in
    web)
        run_migrations
        start_web
        ;;
    bot)
        python -m app.bot.main
        ;;
    worker)
        python -m worker.main
        ;;
    all)
        run_migrations
        start_bot
        start_worker
        start_web
        ;;
    *)
        echo "ERROR: Unsupported SERVICE_ROLE: $SERVICE_ROLE"
        echo "Expected one of: web, bot, worker, all"
        exit 1
        ;;
esac
