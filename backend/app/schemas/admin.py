"""Schemas for Admin API (A1–A13)."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class BotConfigUpdate(BaseModel):
    key: str = Field(..., max_length=100, pattern=r"^[a-z_][a-z0-9_]*$")
    value: Any
    description: str | None = Field(None, max_length=300)


class BotConfigResponse(BaseModel):
    key: str
    value: Any
    description: str | None = None
    updated_at: datetime


class FeatureFlags(BaseModel):
    relance_enabled: bool = True
    maps_enabled: bool = True
    auto_escalation_enabled: bool = True
    club_loyalty_enabled: bool = True
    maintenance_mode: bool = False


class FeatureFlagsResponse(FeatureFlags):
    updated_at: datetime


class AuditLogEntry(BaseModel):
    log_id: uuid.UUID
    actor: str
    action: str
    resource_type: str
    resource_id: str | None = None
    changes: dict[str, Any] | None = None
    timestamp: datetime
    ip_address: str | None = None


class AuditLogListResponse(BaseModel):
    entries: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


class HandoffToggle(BaseModel):
    """Switch a conversation between bot-controlled and hub-team-controlled."""
    conversation_id: uuid.UUID
    mode: str = Field(..., pattern="^(bot|human)$")
    reason: str | None = Field(None, max_length=300)


class HandoffToggleResponse(BaseModel):
    conversation_id: uuid.UUID
    mode: str
    updated_at: datetime


class SystemHealthResponse(BaseModel):
    status: str  # "healthy" | "degraded" | "down"
    components: dict[str, str]
    version: str
    uptime_seconds: float
    checked_at: datetime
