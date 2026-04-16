# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — FastAPI Backend Dockerfile
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

# No system dependencies needed — healthcheck uses Python urllib

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code + Alembic migrations
COPY app/ ./app/
COPY alembic/ ./alembic/
COPY alembic.ini .
COPY entrypoint.sh .
RUN chmod +x entrypoint.sh

# Non-root user for security
RUN useradd -m -u 1001 mbb && chown -R mbb:mbb /app
USER mbb

# Run migrations then start the server
ENTRYPOINT ["./entrypoint.sh"]
# Default command (overridden by docker-compose)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2", "--timeout-graceful-shutdown", "30"]
