"""Schemas for EP-04, EP-05, EP-06, EP-07 (M5 Lead Qualification)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import LeadIntent, LeadScore, LeadStage


class LeadCreate(BaseModel):
    conversation_id: uuid.UUID
    score: LeadScore
    score_value: int = Field(..., ge=0, le=10)
    stage: LeadStage = LeadStage.awareness
    intent: LeadIntent
    product_interest: list[str] = Field(
        ..., max_length=10, description="Max 10 product IDs"
    )
    source: str = Field(..., max_length=50)


class LeadCreatedResponse(BaseModel):
    lead_id: uuid.UUID
    customer_id: str
    score: LeadScore
    score_value: int
    stage: LeadStage
    qualified_at: datetime


class RelanceSummary(BaseModel):
    relance_id: uuid.UUID
    attempt_number: int
    scheduled_at: datetime | None = None
    delivered_at: datetime | None = None
    response_received: bool = False
    hook_type: str | None = None


class CustomerInLead(BaseModel):
    phone_number: str
    name: str | None = None
    city: str | None = None


class LeadResponse(BaseModel):
    lead_id: uuid.UUID
    customer: CustomerInLead
    score: LeadScore
    score_value: int
    stage: LeadStage
    intent: LeadIntent
    product_interest: list[str]
    source: str
    relance_count: int
    relances: list[RelanceSummary]
    qualified_at: datetime
    converted_at: datetime | None = None


class LeadScoreUpdate(BaseModel):
    score: LeadScore
    score_value: int = Field(..., ge=0, le=10)
    reason: str = Field(..., max_length=200)


class LeadScoreResponse(BaseModel):
    lead_id: uuid.UUID
    score: LeadScore
    score_value: int
    updated_at: datetime


class LeadStageUpdate(BaseModel):
    stage: LeadStage
    reason: str = Field(..., max_length=200)


class LeadStageResponse(BaseModel):
    lead_id: uuid.UUID
    stage: LeadStage
    updated_at: datetime
