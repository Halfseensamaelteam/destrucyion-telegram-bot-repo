#!/bin/bash
set -e

# 1. Jalankan migrasi database NeonDB
echo "==> Running database migrations..."
alembic upgrade head

# 2. Jalankan Bot Telegram di background
echo "==> Starting Telegram Bot (app.bot.main)..."
python -m app.bot.main &

# 3. Jalankan Worker Telethon di background
echo "==> Starting Worker (worker.main)..."
python -m worker.main &

# 4. Jalankan Web Service FastAPI di foreground
echo "==> Starting FastAPI Web Service..."
if [ $# -gt 0 ]; then
    exec "$@"
else
    exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
fi