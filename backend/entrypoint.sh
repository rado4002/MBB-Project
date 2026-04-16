#!/bin/sh
# Entrypoint: run Alembic migrations then start the FastAPI server.
# Used by the Docker container on startup.

set -e

echo "[entrypoint] Running database migrations..."
alembic upgrade head

echo "[entrypoint] Starting FastAPI..."
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1
