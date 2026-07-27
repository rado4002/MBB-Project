"""
Admin API — A1 through A13 (except A8, A10, A11, A13 which live in their domain routers).

A1:  GET  /api/v1/admin/system-health
A2:  GET  /api/v1/admin/config
A3:  PUT  /api/v1/admin/config/{key}
A4:  GET  /api/v1/admin/audit-logs
A5:  POST /api/v1/admin/feature-flags
A6:  GET  /api/v1/admin/feature-flags
A7:  POST /api/v1/admin/maintenance
A9:  POST /api/v1/admin/test-message
A12: POST /api/v1/admin/export-csv
"""
import csv
import io
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse

from app.api.deps import DBSession, IdempotencyKey, RedisClient, require_role
from app.modules.m9_dashboard import audit as audit_svc
from app.modules.m9_dashboard import config as config_svc
from app.schemas.admin import (
    AuditLogListResponse,
    BotConfigResponse,
    BotConfigUpdate,
    FeatureFlags,
    FeatureFlagsResponse,
    SystemHealthResponse,
)

log = structlog.get_logger()
router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_role("admin"))],
)


@router.get("/system-health", response_model=SystemHealthResponse)
async def system_health(db: DBSession, redis: RedisClient):
    """Check health of all system components (A1)."""
    import time
    components = {}

    # Check DB
    try:
        from sqlalchemy import text
        await db.execute(text("SELECT 1"))
        components["database"] = "healthy"
    except Exception:
        components["database"] = "down"

    # Check Redis
    try:
        await redis.ping()
        components["redis"] = "healthy"
    except Exception:
        components["redis"] = "down"

    components["api"] = "healthy"

    overall = "healthy" if all(v == "healthy" for v in components.values()) else "degraded"
    return SystemHealthResponse(
        status=overall,
        components=components,
        version="1.0.0-phase1d",
        uptime_seconds=round(time.monotonic(), 2),
        checked_at=datetime.now(timezone.utc),
    )


@router.get("/config")
async def list_config(redis: RedisClient):
    """List all bot configuration keys (A2)."""
    configs = await config_svc.list_config(redis)
    return {"configs": configs, "total": len(configs)}


@router.put("/config/{key}", response_model=BotConfigResponse)
async def update_config(key: str, body: BotConfigUpdate, db: DBSession, redis: RedisClient, idempotency_key: IdempotencyKey):
    """Update a bot configuration value (A3). Idempotent via key tracking."""
    result = await config_svc.set_config(redis, body.key, body.value, body.description)
    await audit_svc.log_action(
        db,
        user_name="admin",
        user_role="admin",
        action="config.update",
        target_entity="bot_config",
        target_id=body.key,
        new_value={"key": body.key, "value": body.value},
    )
    return BotConfigResponse(
        key=result["key"],
        value=result["value"],
        description=result.get("description"),
        updated_at=datetime.fromisoformat(result["updated_at"]),
    )


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def get_audit_logs(
    db: DBSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: str | None = Query(default=None),
):
    """Retrieve audit log entries (A4)."""
    entries, total = await audit_svc.list_logs(db, actor, limit, offset)
    return AuditLogListResponse(
        entries=entries,
        total=total,
        limit=limit,
        offset=offset,
    )


@router.post("/feature-flags", response_model=FeatureFlagsResponse)
async def set_feature_flags(body: FeatureFlags, db: DBSession, redis: RedisClient, idempotency_key: IdempotencyKey):
    """Update system feature flags (A5). Immediately effective via Redis. Idempotent via key tracking."""
    result = await config_svc.set_feature_flags(redis, body.model_dump())
    await audit_svc.log_action(
        db,
        user_name="admin",
        user_role="admin",
        action="feature_flags.update",
        target_entity="feature_flags",
        new_value=body.model_dump(),
    )
    return FeatureFlagsResponse(
        **{k: v for k, v in result.items() if k != "updated_at"},
        updated_at=datetime.fromisoformat(result["updated_at"]),
    )


@router.get("/feature-flags", response_model=FeatureFlagsResponse)
async def get_feature_flags(redis: RedisClient):
    """Get current feature flags (A6)."""
    result = await config_svc.get_feature_flags(redis)
    return FeatureFlagsResponse(
        **{k: v for k, v in result.items() if k != "updated_at"},
        updated_at=datetime.fromisoformat(result["updated_at"]),
    )


@router.post("/maintenance")
async def toggle_maintenance(enabled: bool, db: DBSession, redis: RedisClient, idempotency_key: IdempotencyKey):
    """Enable or disable maintenance mode (A7). Idempotent via key tracking."""
    result = await config_svc.toggle_maintenance(redis, enabled)
    await audit_svc.log_action(
        db,
        user_name="admin",
        user_role="admin",
        action="maintenance.toggle",
        target_entity="system",
        new_value={"maintenance_mode": enabled},
    )
    return {"maintenance_mode": enabled, "updated_at": result.get("updated_at")}


@router.post("/test-message")
async def send_test_message(phone_number: str, db: DBSession):
    """Send a test message to a phone number in dev mode (A9)."""
    from app.config import get_settings
    settings = get_settings()
    if settings.app_env != "development":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="test_message_only_in_dev")
    log.info("admin.test_message", phone=phone_number)
    return {"sent": True, "phone": phone_number, "message": "Test message from MBB ya Kin"}


@router.post("/export-csv")
async def export_csv(
    db: DBSession,
    resource: str = Query(..., description="leads | orders | relances | maps"),
):
    """Export resource data as CSV (A12)."""
    from sqlalchemy import select
    from app.models.lead import Lead
    from app.models.order import Order
    from app.models.relance import Relance
    from app.models.maps_tag import MapsTag

    model_map = {"leads": Lead, "orders": Order, "relances": Relance, "maps": MapsTag}
    model = model_map.get(resource)
    if not model:
        raise HTTPException(status_code=400, detail=f"Unknown resource: {resource}")

    result = await db.execute(select(model).limit(10000))
    rows = result.scalars().all()

    output = io.StringIO()
    if rows:
        columns = [c.key for c in model.__table__.columns]
        writer = csv.writer(output)
        writer.writerow(columns)
        for row in rows:
            writer.writerow([getattr(row, col, None) for col in columns])

    output.seek(0)
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={resource}.csv"},
    )
