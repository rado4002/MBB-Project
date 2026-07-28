"""Safe creation helpers for the additive operator audit ledger."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.operator_audit import (
    OperatorAuditEvent,
    OperatorAuditSecurityMetadata,
)

_FORBIDDEN_METADATA_TERMS = frozenset(
    {
        "password",
        "password_hash",
        "session_token",
        "csrf",
        "idempotency_key",
        "phone",
        "phone_number",
        "message_body",
        "message_content",
        "escalation_free_text",
        "provider_error",
        "ip",
        "ip_address",
        "user_agent",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_metadata(value: Any, *, path: str = "metadata") -> None:
    if isinstance(value, dict):
        for key, nested_value in value.items():
            normalized_key = str(key).strip().lower()
            if normalized_key in _FORBIDDEN_METADATA_TERMS:
                raise ValueError(f"sensitive audit metadata is forbidden at {path}.{key}")
            _validate_metadata(nested_value, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested_value in enumerate(value):
            _validate_metadata(nested_value, path=f"{path}[{index}]")


def retention_deadline(
    category: str,
    *,
    occurred_at: datetime | None = None,
    settings: Settings | None = None,
) -> datetime:
    configured = settings or get_settings()
    if category not in {"business", "security"}:
        raise ValueError("unsupported operator audit category")
    return (occurred_at or _utcnow()) + timedelta(
        days=configured.operator_audit_retention_days
    )


def security_metadata_retention_deadline(
    *,
    occurred_at: datetime | None = None,
    settings: Settings | None = None,
) -> datetime:
    configured = settings or get_settings()
    return (occurred_at or _utcnow()) + timedelta(
        days=configured.operator_security_metadata_retention_days
    )


async def append_operator_audit_event(
    session: AsyncSession,
    *,
    category: str,
    actor_kind: str,
    action: str,
    outcome: str,
    actor_account_id: uuid.UUID | None = None,
    actor_display_name: str | None = None,
    effective_role: str | None = None,
    request_id: str | None = None,
    target_type: str | None = None,
    target_id: str | None = None,
    reason_code: str | None = None,
    failure_code: str | None = None,
    idempotency_reference: str | None = None,
    metadata: dict[str, Any] | None = None,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
    occurred_at: datetime | None = None,
    settings: Settings | None = None,
) -> OperatorAuditEvent:
    """Append one core event and optional already-fingerprinted sensitive metadata."""
    event_time = occurred_at or _utcnow()
    safe_metadata = metadata or {}
    _validate_metadata(safe_metadata)
    event = OperatorAuditEvent(
        occurred_at=event_time,
        category=category,
        actor_kind=actor_kind,
        actor_account_id=actor_account_id,
        actor_display_name=actor_display_name,
        effective_role=effective_role,
        request_id=request_id,
        action=action,
        target_type=target_type,
        target_id=target_id,
        reason_code=reason_code,
        outcome=outcome,
        failure_code=failure_code,
        idempotency_reference=idempotency_reference,
        event_metadata=safe_metadata,
        retain_until=retention_deadline(
            category, occurred_at=event_time, settings=settings
        ),
    )
    session.add(event)
    await session.flush()

    if source_network_fingerprint or user_agent_fingerprint:
        session.add(
            OperatorAuditSecurityMetadata(
                event_id=event.event_id,
                source_network_fingerprint=source_network_fingerprint,
                user_agent_fingerprint=user_agent_fingerprint,
                retain_until=security_metadata_retention_deadline(
                    occurred_at=event_time, settings=settings
                ),
            )
        )
        await session.flush()
    return event
