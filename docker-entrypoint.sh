#!/bin/bash
set -e

# Only run migrations if we are starting the API
if [ "$1" = 'uvicorn' ] || [ "$1" = 'pytest' ]; then
    echo "Running database migrations..."
    alembic upgrade head
fi

exec "$@"
