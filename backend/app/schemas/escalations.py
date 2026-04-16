"""Schemas for EP-08 escalations (M8 Escalation System)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import EscalationPriority, EscalationStatus


class EscalationCreate(BaseModel):
    conversation_id: uuid.UUID
    priority: EscalationPriority = EscalationPriority.medium
    reason: str = Field(..., max_length=500)
    escalation_type: str = Field(
        ...,
        max_length=50,
        description="voice_note | complex_issue | high_value_lead | payment_issue",
    )


class EscalationResponse(BaseModel):
    escalation_id: uuid.UUID
    conversation_id: uuid.UUID
    priority: EscalationPriority
    status: EscalationStatus
    reason: str
    escalation_type: str
    assigned_to: str | None = None
    created_at: datetime
    updated_at: datetime


class EscalationResolve(BaseModel):
    resolution_note: str = Field(..., max_length=1000)
    resolved_by: str = Field(..., max_length=100)


class EscalationResolveResponse(BaseModel):
    escalation_id: uuid.UUID
    status: EscalationStatus
    resolved_at: datetime
