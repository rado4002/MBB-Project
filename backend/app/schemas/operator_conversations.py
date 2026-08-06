"""Minimized browser-operator conversation read contracts."""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

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
    operator_author: OperatorHumanOwner | None = None
    delivery_state: Literal["accepted", "sent", "failed", "uncertain"] | None = None
    delivery_state_timestamp: datetime | None = None
    content_type: ContentType
    text: str | None
    media: OperatorMessageMedia | None
    language: Language


class OperatorMessageHistoryResponse(BaseModel):
    items: list[OperatorMessageItem]
    next_older_cursor: str | None


class OperatorReplyCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4096)
    expected_ownership_version: int = Field(gt=0)

    @field_validator("text")
    @classmethod
    def reject_blank_message(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reply text must not be blank")
        return value


class OperatorInternalNoteCreate(BaseModel):
    text: str = Field(min_length=1, max_length=4096)

    @field_validator("text")
    @classmethod
    def reject_blank_note(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("internal note text must not be blank")
        return value


class OperatorInternalNoteItem(BaseModel):
    kind: Literal["internal_note"] = "internal_note"
    note_id: UUID
    occurred_at: datetime
    author: OperatorHumanOwner
    text: str


class OperatorTimelineMessageItem(OperatorMessageItem):
    kind: Literal["message"] = "message"


OperatorTimelineItem = Annotated[
    OperatorTimelineMessageItem | OperatorInternalNoteItem,
    Field(discriminator="kind"),
]


class OperatorTimelineResponse(BaseModel):
    items: list[OperatorTimelineItem]
    next_older_cursor: str | None
