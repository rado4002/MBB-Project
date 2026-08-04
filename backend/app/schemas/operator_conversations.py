"""Minimized browser-operator conversation read contracts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.schemas.common import ContentType, ConversationStatus, Language


class OperatorCustomerSummary(BaseModel):
    display_name: str | None = None
    phone_masked: str


class OperatorLatestMessage(BaseModel):
    preview: str
    content_type: ContentType
    direction: Literal["inbound", "outbound"]
    occurred_at: datetime


class OperatorOpenEscalation(BaseModel):
    exists: bool


class OperatorHumanOwner(BaseModel):
    account_id: UUID
    display_name: str


class OperatorConversationOwnership(BaseModel):
    owner_type: Literal["ai", "human"]
    human_owner: OperatorHumanOwner | None
    ai_execution_state: Literal["eligible", "paused"]
    version: int = Field(gt=0)
    updated_at: datetime


class OperatorConversationQueueItem(BaseModel):
    conversation_id: UUID
    customer: OperatorCustomerSummary
    language: Language
    status: ConversationStatus
    message_count: int = Field(ge=0)
    latest_message: OperatorLatestMessage | None
    awaiting_response_since: datetime | None
    open_escalation: OperatorOpenEscalation
    ownership: OperatorConversationOwnership


class OperatorConversationQueueResponse(BaseModel):
    items: list[OperatorConversationQueueItem]
    next_cursor: str | None


class OperatorLeadSummary(BaseModel):
    score: str | None = None
    stage: str | None = None
    intent: str | None = None
    product_interests: list[str] = Field(default_factory=list, max_length=5)


class OperatorConversationDetail(BaseModel):
    conversation_id: UUID
    status: ConversationStatus
    language: Language
    message_count: int = Field(ge=0)
    updated_at: datetime
    customer: OperatorCustomerSummary
    lead: OperatorLeadSummary | None
    open_escalation: OperatorOpenEscalation
    ownership: OperatorConversationOwnership


class OperatorOwnershipTransitionRequest(BaseModel):
    target_owner_type: Literal["ai", "human"]
    expected_version: int = Field(gt=0)


class OperatorOwnershipTransitionResponse(BaseModel):
    conversation_id: UUID
    ownership: OperatorConversationOwnership


class OperatorMessageMedia(BaseModel):
    kind: Literal["voice_note", "image"]
    available: Literal[False] = False


class OperatorMessageItem(BaseModel):
    message_id: UUID
    occurred_at: datetime
    direction: Literal["inbound", "outbound"]
    sender_type: Literal["customer", "operator", "system", "unknown"]
    content_type: ContentType
    text: str | None
    media: OperatorMessageMedia | None
    language: Language


class OperatorMessageHistoryResponse(BaseModel):
    items: list[OperatorMessageItem]
    next_older_cursor: str | None
