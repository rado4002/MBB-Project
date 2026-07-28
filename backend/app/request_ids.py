"""Strict request-ID normalization shared by middleware and dependencies."""

from __future__ import annotations

import re
import uuid

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def normalize_or_generate_request_id(value: str | None) -> str:
    if value and REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid.uuid4())
