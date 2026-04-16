"""Schemas for EP-08, EP-09, EP-10 (M6 Relance Engine)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class RelanceCreate(BaseModel):
    lead_id: uuid.UUID
    attempt_number: int = Field(..., ge=1, le=3)
    hook_type: str = Field(
        ...,
        max_length=50,
        description="Persuasion hook: reciprocity | social_proof | scarcity | ...",
    )
    scheduled_at: datetime


class RelanceResponse(BaseModel):
    relance_id: uuid.UUID
    lead_id: uuid.UUID
    attempt_number: int
    hook_type: str
    scheduled_at: datetime
    delivered_at: datetime | None = None
    response_received: bool = False
    created_at: datetime


class RelanceDeliverUpdate(BaseModel):
    delivered_at: datetime


class RelanceDeliverResponse(BaseModel):
    relance_id: uuid.UUID
    delivered_at: datetime


class RelanceResponseUpdate(BaseModel):
    response_received: bool
    response_content: str | None = Field(None, max_length=4096)


class RelanceResponseUpdateResponse(BaseModel):
    relance_id: uuid.UUID
    response_received: bool
    updated_at: datetime
