"""
app/modules/m9_dashboard/audit.py — Append-only audit log operations.

Every admin/hub/lab write operation is logged here.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.admin_audit_log import AdminAuditLog

log = structlog.get_logger(__name__)


async def log_action(
    session: AsyncSession,
    *,
    user_name: str,
    user_role: str,
    action: str,
    target_entity: str,
    target_id: str | None = None,
    old_value: dict | None = None,
    new_value: dict | None = None,
    justification: str | None = None,
    ip_address: str | None = None,
) -> dict[str, Any]:
    """Append an audit log entry. Never fails silently — raises on DB error."""
    entry = AdminAuditLog(
        user_name=user_name,
        user_role=user_role,
        action=action,
        target_entity=target_entity,
        target_id=target_id,
        old_value=old_value,
        new_value=new_value,
        justification=justification,
        ip_address=ip_address,
    )
    session.add(entry)
    await session.flush()
    log.info(
        "audit.logged",
        action=action,
        target=target_entity,
        user=user_name,
    )
    return {
        "audit_id": str(entry.audit_id),
        "action": action,
        "target_entity": target_entity,
        "created_at": entry.created_at.isoformat() if entry.created_at else datetime.now(timezone.utc).isoformat(),
    }


async def list_logs(
    session: AsyncSession,
    actor: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """List audit log entries with optional actor filter."""
    base = select(AdminAuditLog)
    count_q = select(func.count()).select_from(AdminAuditLog)

    if actor:
        base = base.where(AdminAuditLog.user_name == actor)
        count_q = count_q.where(AdminAuditLog.user_name == actor)

    total = (await session.execute(count_q)).scalar() or 0
    result = await session.execute(
        base.order_by(AdminAuditLog.created_at.desc()).offset(offset).limit(limit)
    )
    entries = result.scalars().all()

    return (
        [
            {
                "log_id": str(e.audit_id),
                "actor": e.user_name,
                "action": e.action,
                "resource_type": e.target_entity,
                "resource_id": e.target_id,
                "changes": e.new_value,
                "timestamp": e.created_at.isoformat() if e.created_at else None,
                "ip_address": str(e.ip_address) if e.ip_address else None,
            }
            for e in entries
        ],
        total,
    )
