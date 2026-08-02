"""
Request middleware:
- X-Request-ID header injection (passthrough or generate)
- X-Response-Time header injection
- Maintenance mode check (reads Redis flag)
"""
import time

import structlog
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.request_ids import normalize_or_generate_request_id

log = structlog.get_logger()


class RequestTracingMiddleware(BaseHTTPMiddleware):
    """Injects X-Request-ID and X-Response-Time into every response."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = normalize_or_generate_request_id(
            request.headers.get("X-Request-ID")
        )
        request.state.request_id = request_id
        start = time.perf_counter()

        # Bind request_id to structlog context for this coroutine
        with structlog.contextvars.bound_contextvars(request_id=request_id):
            response = await call_next(request)

        elapsed_ms = round((time.perf_counter() - start) * 1000, 2)
        response.headers["X-Request-ID"] = request_id
        response.headers["X-Response-Time"] = f"{elapsed_ms}ms"
        return response


class MaintenanceModeMiddleware(BaseHTTPMiddleware):
    """
    Returns 503 on all non-admin, non-health endpoints when maintenance mode is active.
    Flag stored in Redis key: `system:maintenance`.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        path = request.url.path
        # Always allow health checks and admin paths through
        if path in ("/health", "/api/v1/health") or path.startswith("/api/v1/admin"):
            return await call_next(request)

        # Lazy redis access — app state is set during lifespan
        redis = getattr(request.app.state, "redis", None)
        if redis is not None:
            try:
                maintenance = await redis.get("system:maintenance")
                if maintenance in (b"1", "1"):
                    if path.startswith("/api/v1/operator/"):
                        from app.api.browser_auth_errors import (
                            BrowserAuthError,
                            browser_error_response,
                        )

                        return browser_error_response(
                            request,
                            BrowserAuthError(
                                status_code=503,
                                code="MAINTENANCE_MODE",
                                message="The service is temporarily in maintenance mode.",
                            ),
                        )
                    from fastapi.responses import JSONResponse
                    return JSONResponse(
                        status_code=503,
                        content={"error": "maintenance_mode", "message": "Système en maintenance. Revenez bientôt."},
                    )
            except Exception:
                pass  # Redis unavailable — don't block traffic

        return await call_next(request)
