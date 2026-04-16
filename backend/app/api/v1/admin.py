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
import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DBSession, RedisClient, require_role
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
    log.info("admin.system_health")
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A1 not yet implemented")


@router.get("/config")
async def list_config(db: DBSession):
    """List all bot configuration keys (A2)."""
    log.info("admin.config.list")
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A2 not yet implemented")


@router.put("/config/{key}", response_model=BotConfigResponse)
async def update_config(key: str, body: BotConfigUpdate, db: DBSession):
    """Update a bot configuration value (A3)."""
    log.info("admin.config.update", key=key)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A3 not yet implemented")


@router.get("/audit-logs", response_model=AuditLogListResponse)
async def get_audit_logs(
    db: DBSession,
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    actor: str | None = Query(default=None),
):
    """Retrieve audit log entries (A4)."""
    log.info("admin.audit_logs", actor=actor)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A4 not yet implemented")


@router.post("/feature-flags", response_model=FeatureFlagsResponse)
async def set_feature_flags(body: FeatureFlags, db: DBSession, redis: RedisClient):
    """Update system feature flags (A5). Immediately effective via Redis."""
    log.info("admin.feature_flags.set")
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A5 not yet implemented")


@router.get("/feature-flags", response_model=FeatureFlagsResponse)
async def get_feature_flags(redis: RedisClient):
    """Get current feature flags (A6)."""
    log.info("admin.feature_flags.get")
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A6 not yet implemented")


@router.post("/maintenance")
async def toggle_maintenance(enabled: bool, redis: RedisClient):
    """Enable or disable maintenance mode (A7). Routes 503 to all non-admin endpoints."""
    log.info("admin.maintenance.toggle", enabled=enabled)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A7 not yet implemented")


@router.post("/test-message")
async def send_test_message(phone_number: str, db: DBSession):
    """Send a test message to a phone number in dev mode (A9)."""
    log.info("admin.test_message", phone=phone_number)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A9 not yet implemented")


@router.post("/export-csv")
async def export_csv(
    db: DBSession,
    resource: str = Query(..., description="leads | orders | relances | maps"),
):
    """Export resource data as CSV for Sheets sync (A12)."""
    log.info("admin.export_csv", resource=resource)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A12 not yet implemented")
