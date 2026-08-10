"""Schemas for EP-02, EP-03, EP-17, EP-18, EP-19 (M4 Conversation Engine)."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import (
    ContentType,
    ConversationStatus,
    Language,
    LeadScore,
    LeadStage,
    PaginationMeta,
)


class CustomerSummary(BaseModel):
    phone_number: str
    name: str | None = None
    city: str | None = None
    language: Language | None = None
    club_member: bool = False


class MessageItem(BaseModel):
    message_id: uuid.UUID
    timestamp: datetime
    direction: str  # "inbound" | "outbound"
    content: str
    content_type: ContentType
    language: Language | None = None


class LeadInConversation(BaseModel):
    lead_id: uuid.UUID | None = None
    score: LeadScore | None = None
    stage: LeadStage | None = None
    relance_count: int = 0


class ConversationResponse(BaseModel):
    conversation_id: uuid.UUID
    customer: CustomerSummary
    status: ConversationStatus
    language_detected: Language | None = None
    context: dict[str, Any] | None = None
    message_count: int
    messages: list[MessageItem]
    lead: LeadInConversation | None = None
    pagination: PaginationMeta


class ConversationContextUpdate(BaseModel):
    context: dict[str, Any] = Field(
        ..., description="Arbitrary JSONB context object. Max size: 10 KB."
    )

    @field_validator("context")
    @classmethod
    def max_size(cls, v: dict) -> dict:
        import json
        if "commercial_state" in v:
            raise ValueError(
                "commercial_state is reserved for the typed commercial-state service"
            )
        if len(json.dumps(v).encode()) > 10_240:
            raise ValueError("Context object exceeds 10 KB limit")
        return v


class ConversationContextResponse(BaseModel):
    conversation_id: uuid.UUID
    context: dict[str, Any]
    updated_at: datetime
