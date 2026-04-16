# ─────────────────────────────────────────────────────────────────────────────
# MBB ya Kin — Streamlit Dashboard Dockerfile
# ─────────────────────────────────────────────────────────────────────────────

FROM python:3.12-slim

# No system dependencies needed — healthcheck uses Python urllib

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/

RUN useradd -m -u 1001 mbb && chown -R mbb:mbb /app
USER mbb

HEALTHCHECK --interval=30s --timeout=10s --start-period=40s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8501/_stcore/health')" || exit 1

CMD ["streamlit", "run", "app/main.py", \
     "--server.port=8501", \
     "--server.address=0.0.0.0", \
     "--server.headless=true", \
     "--server.enableCORS=false", \
     "--server.enableXsrfProtection=true"]
