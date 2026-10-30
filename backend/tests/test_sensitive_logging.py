from __future__ import annotations

import logging
from pathlib import Path

import pytest
from starlette.requests import Request

from app import main
from app.sensitive_logging import (
    SensitiveAccessPathFilter,
    redact_sensitive_log_event,
    sanitize_customer_access_path,
)


SENSITIVE_MARKER = "synthetic-customer-identity"
SYNTHETIC_PHONE = "+999123456789"


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"/api/v1/customers/{SENSITIVE_MARKER}",
            "/api/v1/customers/{customer}",
        ),
        (
            f"/api/v1/customers/{SENSITIVE_MARKER}/language?trace={SENSITIVE_MARKER}",
            "/api/v1/customers/{customer}/language",
        ),
        (
            f"/api/v1/customers/{SENSITIVE_MARKER}/opt-out",
            "/api/v1/customers/{customer}/opt-out",
        ),
    ],
)
def test_customer_access_path_sanitizer(path: str, expected: str) -> None:
    sanitized = sanitize_customer_access_path(path)
    assert sanitized == expected
    assert SENSITIVE_MARKER not in sanitized


def test_uvicorn_access_record_is_sanitized_before_formatting() -> None:
    record = logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=(
            "local-client",
            "PUT",
            f"/api/v1/customers/{SENSITIVE_MARKER}/opt-out?source={SENSITIVE_MARKER}",
            "1.1",
            403,
        ),
        exc_info=None,
    )
    assert SensitiveAccessPathFilter().filter(record) is True
    rendered = record.getMessage()
    assert SENSITIVE_MARKER not in rendered
    assert "/api/v1/customers/{customer}/opt-out" in rendered


def test_structured_application_log_processor_redacts_phone_values() -> None:
    event = redact_sensitive_log_event(
        None,
        "info",
        {
            "event": "synthetic",
            "phone": SYNTHETIC_PHONE,
            "message": f"Customer {SYNTHETIC_PHONE}",
            "nested": {"customer_phone": SYNTHETIC_PHONE},
        },
    )
    assert SYNTHETIC_PHONE not in str(event)
    assert event["phone"] == "[REDACTED_PHONE]"
    assert event["message"] == "Customer [REDACTED_PHONE]"
    assert event["nested"] == {"customer_phone": "[REDACTED_PHONE]"}


@pytest.mark.asyncio
async def test_application_exception_log_redacts_customer_path_and_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CaptureLog:
        def error(self, event: str, **values: object) -> None:
            captured.update(event=event, **values)

    monkeypatch.setattr(main, "log", CaptureLog())
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": f"/api/v1/customers/{SENSITIVE_MARKER}",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("operator.example", 443),
        }
    )
    response = await main.unhandled_exception_handler(
        request, RuntimeError(SENSITIVE_MARKER)
    )
    assert response.status_code == 500
    assert captured == {
        "event": "unhandled_exception",
        "path": "/api/v1/customers/{customer}",
        "error": "RuntimeError",
    }


def test_nginx_access_formats_use_sanitized_customer_route() -> None:
    root = Path(__file__).resolve().parents[2]
    for relative_path in (
        "nginx/nginx.conf",
        "nginx/nginx.recovery-dryrun.conf",
    ):
        content = (root / relative_path).read_text(encoding="utf-8")
        assert "$safe_request_uri" in content
        assert "/api/v1/customers/{customer}/opt-out" in content
        assert "/api/v1/customers/{customer}/language" in content
        assert '"uri":"$request_uri"' not in content
        assert '"$request"' not in content

    for relative_path in (
        "nginx/conf.d/mbb.conf",
        "nginx/conf.d/mbb.ssl.conf",
        "nginx/conf.d/mbb.ssl.conf.prod",
        "nginx/conf.d/mbb.recovery-dryrun.conf",
    ):
        content = (root / relative_path).read_text(encoding="utf-8")
        assert "location ^~ /api/v1/customers/" in content
        assert "error_log /dev/null emerg;" in content
