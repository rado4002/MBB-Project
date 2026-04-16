"""Schemas for EP-15, EP-16 and admin A10, A11 (M8 MAPS Intelligence)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.schemas.common import Language, MapsCategory


class MapsTagCreate(BaseModel):
    conversation_id: uuid.UUID
    category: MapsCategory
    tag: str = Field(..., max_length=100)
    language: Language
    raw_signal: str | None = Field(None, max_length=500)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class MapsTagResponse(BaseModel):
    tag_id: uuid.UUID
    conversation_id: uuid.UUID
    category: MapsCategory
    tag: str
    language: Language
    confidence: float
    created_at: datetime


class MapsInsightItem(BaseModel):
    tag: str
    category: MapsCategory
    count: int
    trend: str | None = None  # "rising" | "falling" | "stable"


class MapsInsightsResponse(BaseModel):
    period_start: datetime
    period_end: datetime
    total_tags: int
    insights: list[MapsInsightItem]


class MapsTagValidate(BaseModel):
    tag_id: uuid.UUID
    validated: bool
    corrected_tag: str | None = Field(None, max_length=100)
    corrected_category: MapsCategory | None = None


class MapsTagValidateResponse(BaseModel):
    tag_id: uuid.UUID
    validated: bool
    updated_at: datetime
