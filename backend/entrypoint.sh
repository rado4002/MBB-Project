#!/bin/sh
# Entrypoint: run Alembic migrations (API container only), then exec CMD.
# Celery workers and Beat scheduler skip migrations.

set -e

# Only the API container should run migrations
# (celery workers/beat pass a command starting with "celery")
FIRST_ARG="${1:-}"
case "$FIRST_ARG" in
    celery)
        echo "[entrypoint] Celery process — skipping migrations"
        ;;
    *)
        echo "[entrypoint] Running database migrations..."
        alembic upgrade head
        echo "[entrypoint] Migrations complete."
        ;;
esac

echo "[entrypoint] Starting: $@"
exec "$@"
