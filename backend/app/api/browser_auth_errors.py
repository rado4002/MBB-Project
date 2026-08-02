"""Stable, non-sensitive error responses for browser authentication."""

from __future__ import annotations

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import JSONResponse

from app.request_ids import normalize_or_generate_request_id


class BrowserAuthError(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retry_after_seconds: int | None = None,
        operator_code: str | None = None,
    ) -> None:
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after_seconds = retry_after_seconds
        self.operator_code = operator_code


def browser_error_response(request: Request, exc: BrowserAuthError) -> JSONResponse:
    request_id = getattr(request.state, "request_id", None)
    request_id = normalize_or_generate_request_id(
        request_id or request.headers.get("X-Request-ID")
    )
    is_operator_route = request.url.path.startswith("/api/v1/operator/")
    operator_code_map = {
        "browser_auth_disabled": "SERVICE_UNAVAILABLE",
        "authentication_unavailable": "SERVICE_UNAVAILABLE",
        "capability_required": "FORBIDDEN",
        "session_required": "AUTH_SESSION_EXPIRED",
        "session_invalid": "AUTH_SESSION_EXPIRED",
    }
    code = exc.code
    if is_operator_route:
        code = exc.operator_code or operator_code_map.get(exc.code, exc.code)
    detail: dict[str, str | int] = {
        "code": code,
        "message": exc.message,
        "request_id": request_id,
    }
    headers = {"Cache-Control": "no-store"}
    if exc.retry_after_seconds is not None:
        detail["retry_after_seconds"] = exc.retry_after_seconds
        headers["Retry-After"] = str(exc.retry_after_seconds)
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": detail},
        headers=headers,
    )


async def browser_validation_error_response(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    if request.url.path.startswith("/api/v1/operator/"):
        return browser_error_response(
            request,
            BrowserAuthError(
                status_code=422,
                code="VALIDATION_ERROR",
                message="The request parameters are invalid.",
            ),
        )
    if not request.url.path.startswith("/api/v1/auth/"):
        return await request_validation_exception_handler(request, exc)
    return browser_error_response(
        request,
        BrowserAuthError(
            status_code=422,
            code="request_invalid",
            message="The authentication request is invalid.",
        ),
    )
