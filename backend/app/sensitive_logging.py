"""Redact phone-addressed legacy Customer paths from MBB access logs."""

from __future__ import annotations

import logging
import re
from typing import Any


_LEGACY_CUSTOMER_PATH = re.compile(
    r"/api/v1/customers/[^/?\s\"]+"
    r"(?P<suffix>/(?:opt-out|language))?"
    r"(?:\?[^\s\"]*)?"
    r"(?=(?:\s|\"|$))"
)
_E164_VALUE = re.compile(r"(?<!\d)\+[1-9][0-9]{6,14}(?!\d)")
_PHONE_KEYS = frozenset({"phone", "phone_number", "customer_phone"})


def sanitize_customer_access_path(value: str) -> str:
    """Replace a legacy Customer identity and query with a route template."""

    def replacement(match: re.Match[str]) -> str:
        return f"/api/v1/customers/{{customer}}{match.group('suffix') or ''}"

    return _LEGACY_CUSTOMER_PATH.sub(replacement, value)


def redact_sensitive_log_event(
    _logger: Any, _method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Redact canonical phone values and legacy paths from structured logs."""

    for key, value in event_dict.items():
        event_dict[key] = _redact_log_value(value, key=str(key).casefold())
    return event_dict


def _redact_log_value(value: Any, *, key: str | None = None) -> Any:
    if key in _PHONE_KEYS:
        return "[REDACTED_PHONE]"
    if isinstance(value, str):
        sanitized = sanitize_customer_access_path(value)
        return _E164_VALUE.sub("[REDACTED_PHONE]", sanitized)
    if isinstance(value, dict):
        return {
            nested_key: _redact_log_value(
                nested_value, key=str(nested_key).casefold()
            )
            for nested_key, nested_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_log_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_log_value(item) for item in value)
    return value


class SensitiveAccessPathFilter(logging.Filter):
    """Sanitize Uvicorn access-record arguments before they are formatted."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = sanitize_customer_access_path(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(_sanitize_argument(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {
                key: _sanitize_argument(value) for key, value in record.args.items()
            }
        return True


def _sanitize_argument(value: Any) -> Any:
    return sanitize_customer_access_path(value) if isinstance(value, str) else value


def configure_sensitive_access_log_redaction() -> None:
    """Install one process-local filter on Uvicorn's access logger."""

    access_logger = logging.getLogger("uvicorn.access")
    if not any(
        isinstance(existing, SensitiveAccessPathFilter)
        for existing in access_logger.filters
    ):
        access_logger.addFilter(SensitiveAccessPathFilter())
