import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import get_settings

settings = get_settings()

# ── Structured JSON Logging ───────────────────────────────────────────────────
structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(
        logging.DEBUG if settings.debug else logging.INFO
    ),
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),
)

log = structlog.get_logger()


# ── Lifespan ─────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    log.info(
        "mbb_startup",
        env=settings.app_env,
        whatsapp_mode=settings.whatsapp_mode,
        ai_adapter=settings.ai_adapter,
        crm_adapter=settings.crm_adapter,
    )
    yield
    log.info("mbb_shutdown")


# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(
    title="MBB ya Kin API",
    description="Multi-Language WhatsApp Lead Nurturer Bot — DRC",
    version="1.0.0",
    docs_url="/api/docs" if settings.debug else None,
    redoc_url="/api/redoc" if settings.debug else None,
    openapi_url="/api/openapi.json" if settings.debug else None,
    lifespan=lifespan,
)

# ── Prometheus Metrics ────────────────────────────────────────────────────────
Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    should_respect_env_var=True,
    env_var_name="ENABLE_METRICS",
    excluded_handlers=["/health", "/metrics"],
).instrument(app).expose(app, endpoint="/metrics", include_in_schema=False)


# ── Global Exception Handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_server_error", "message": "An unexpected error occurred."},
    )


# ── System Endpoints ──────────────────────────────────────────────────────────
@app.get("/health", tags=["system"], include_in_schema=False)
async def health():
    return {
        "status": "ok",
        "env": settings.app_env,
        "whatsapp_mode": settings.whatsapp_mode,
        "ai_adapter": settings.ai_adapter,
        "crm_adapter": settings.crm_adapter,
    }


@app.get("/api/v1/health", tags=["system"])
async def api_health():
    return {"status": "ok", "version": "1.0.0"}
