"""Schemas for EP-01: POST /api/v1/messages (M1 Gateway)."""
import re
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ContentType, Language

_E164_PHONE_RE = re.compile(r"^\+[1-9][0-9]{6,14}$")


def validate_customer_phone_value(value: str) -> str:
    if not isinstance(value, str) or not _E164_PHONE_RE.fullmatch(value):
        raise ValueError(
            "Phone must be canonical international E.164: "
            "leading + and 7 to 15 digits, starting with 1-9"
        )
    return value


def validate_whatsapp_message_id_value(value: str) -> str:
    if not value.strip() or value != value.strip():
        raise ValueError("WhatsApp message ID must be non-empty without surrounding whitespace")
    return value


class InboundMessageRequest(BaseModel):
    message_id: uuid.UUID
    customer_phone: str = Field(..., description="Canonical international E.164 phone")
    content: str = Field(..., min_length=1, max_length=4096)
    content_type: ContentType
    timestamp: datetime
    whatsapp_message_id: str = Field(..., min_length=1, max_length=100)

    @field_validator("customer_phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        return validate_customer_phone_value(v)

    @field_validator("whatsapp_message_id")
    @classmethod
    def validate_whatsapp_message_id(cls, v: str) -> str:
        return validate_whatsapp_message_id_value(v)

    @field_validator("timestamp")
    @classmethod
    def not_future(cls, v: datetime) -> datetime:
        now = datetime.now(timezone.utc)
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        diff = (v - now).total_seconds()
        if diff > 300:  # 5-minute clock-skew tolerance
            raise ValueError("Timestamp cannot be more than 5 minutes in the future")
        return v


class MessageResponsePayload(BaseModel):
    content: str
    language: Language
    processing_time_ms: int


class LeadSummary(BaseModel):
    lead_id: uuid.UUID | None = None
    score: str | None = None
    stage: str | None = None


class InboundMessageResponse(BaseModel):
    status: str  # "processed" | "queued"
    conversation_id: uuid.UUID | None = None
    response: MessageResponsePayload | None = None
    lead: LeadSummary | None = None
    maps_tags_created: int = 0


class QueuedMessageResponse(BaseModel):
    status: str = "queued"
    queue_position: int
    estimated_processing_seconds: int


class MessageHistoryItem(BaseModel):
    message_id: uuid.UUID
    direction: str
    content: str
    content_type: str
    language: str
    timestamp: datetime
    processing_time_ms: int | None = None

    model_config = {"from_attributes": True}


class MessageHistoryResponse(BaseModel):
    conversation_id: uuid.UUID
    messages: list[MessageHistoryItem]
    total: int
