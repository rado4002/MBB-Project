import logging
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import text

from app.api.browser_auth_errors import (
    BrowserAuthError,
    browser_error_response,
    browser_validation_error_response,
)
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
    # Attach Redis client to app.state for middleware access
    from app.redis_client import get_redis_pool
    import redis.asyncio as aioredis
    app.state.redis = aioredis.Redis(connection_pool=get_redis_pool())
    app.state.shutting_down = False
    yield
    # ── Graceful shutdown ─────────────────────────────────────────────────────
    app.state.shutting_down = True
    log.info("mbb_shutdown.starting", msg="Finishing in-flight requests...")
    # Uvicorn handles waiting for in-flight requests via --timeout-graceful-shutdown.
    # We clean up our resources after they complete.
    await app.state.redis.aclose()
    log.info("mbb_shutdown.complete")


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
@app.exception_handler(BrowserAuthError)
async def browser_auth_exception_handler(request: Request, exc: BrowserAuthError):
    return browser_error_response(request, exc)


@app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
):
    return await browser_validation_error_response(request, exc)


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    log.error("unhandled_exception", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": "internal_server_error", "message": "An unexpected error occurred."},
    )


# ── System Endpoints ──────────────────────────────────────────────────────────
async def _liveness_checks(request: Request) -> tuple[dict, list[str]]:
    """Run Redis + PostgreSQL + Celery + Baileys liveness probes."""
    checks: dict = {"redis": False, "database": False, "celery": False, "baileys": False}
    errors: list[str] = []

    # Redis ping (uses the pool attached during lifespan startup)
    try:
        await request.app.state.redis.ping()
        checks["redis"] = True
    except AttributeError:
        errors.append("redis: not initialised (startup incomplete)")
    except Exception as exc:
        errors.append(f"redis: {exc}")

    # PostgreSQL ping — lightweight SELECT 1
    try:
        from app.database import async_session_factory
        async with async_session_factory() as session:
            await session.execute(text("SELECT 1"))
        checks["database"] = True
    except Exception as exc:
        errors.append(f"database: {exc}")

    # Celery — check broker connectivity via Redis ping on DB 0
    try:
        import redis.asyncio as aioredis
        broker = aioredis.from_url(settings.celery_broker_url, socket_timeout=3)
        await broker.ping()
        await broker.aclose()
        checks["celery"] = True
    except Exception as exc:
        errors.append(f"celery: broker unreachable — {exc}")

    # Baileys bridge — HTTP GET /health (best-effort, only in baileys mode)
    if settings.whatsapp_mode == "baileys":
        try:
            import httpx
            async with httpx.AsyncClient(timeout=httpx.Timeout(5.0)) as client:
                resp = await client.get(f"{settings.baileys_url}/health")
                checks["baileys"] = resp.is_success
        except Exception as exc:
            errors.append(f"baileys: {exc}")
    else:
        checks["baileys"] = True  # not applicable in official mode

    return checks, errors


@app.get("/health", tags=["system"], include_in_schema=False)
async def health(request: Request):
    """Docker Compose healthcheck target — returns 503 if Redis or PostgreSQL is down."""
    from app.redis_client import blackout_queue_length

    checks, errors = await _liveness_checks(request)
    queue_depth = await blackout_queue_length()
    all_ok = all(checks.values())

    body = {
        "status": "ok" if all_ok else "degraded",
        "env": settings.app_env,
        "whatsapp_mode": settings.whatsapp_mode,
        "ai_adapter": settings.ai_adapter,
        "crm_adapter": settings.crm_adapter,
        "checks": checks,
        "blackout_queue_depth": queue_depth,
    }
    if not all_ok:
        log.warning("health.degraded", errors=errors)
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)
    return body


@app.get("/api/v1/health", tags=["system"])
async def api_health(request: Request):
    """Structured health check for API consumers — mirrors /health."""
    from app.redis_client import blackout_queue_length

    checks, errors = await _liveness_checks(request)
    queue_depth = await blackout_queue_length()
    all_ok = all(checks.values())

    body = {
        "status": "ok" if all_ok else "degraded",
        "version": "1.0.0",
        "checks": checks,
        "blackout_queue_depth": queue_depth,
    }
    if not all_ok:
        return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, content=body)
    return body


# ── Routers ───────────────────────────────────────────────────────────────────
from app.api.v1 import (  # noqa: E402  (after app creation intentional)
    admin,
    analytics,
    auth,
    conversations,
    customers,
    leads,
    maps,
    messages,
    orders,
    payments,
    relances,
)

_V1_PREFIX = "/api/v1"

app.include_router(messages.router, prefix=_V1_PREFIX)
app.include_router(conversations.router, prefix=_V1_PREFIX)
app.include_router(leads.router, prefix=_V1_PREFIX)
app.include_router(relances.router, prefix=_V1_PREFIX)
app.include_router(orders.router, prefix=_V1_PREFIX)
app.include_router(payments.router, prefix=_V1_PREFIX)
app.include_router(maps.router, prefix=_V1_PREFIX)
app.include_router(customers.router, prefix=_V1_PREFIX)
app.include_router(analytics.router, prefix=_V1_PREFIX)
app.include_router(admin.router, prefix=_V1_PREFIX)
app.include_router(auth.router, prefix=_V1_PREFIX)


# ── Middleware (added AFTER routers so order is correct) ──────────────────────
from app.middleware import MaintenanceModeMiddleware, RequestTracingMiddleware  # noqa: E402

app.add_middleware(MaintenanceModeMiddleware)
app.add_middleware(RequestTracingMiddleware)

# CORS — internal VPS only (Streamlit dashboard on same host)
_cors_origins = ["http://localhost:8501", "http://dashboard:8501"]
if settings.browser_allowed_origin:
    _cors_origins.append(settings.browser_allowed_origin)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT"],
    allow_headers=["*"],
)
