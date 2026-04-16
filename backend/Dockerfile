# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — FastAPI Backend Dockerfile
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim AS base

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies first (layer cache)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/

# Non-root user for security
RUN useradd -m -u 1001 mbb && chown -R mbb:mbb /app
USER mbb

# Default: production API server
# Override in docker-compose.dev.yml for hot-reload
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
