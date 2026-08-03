"""Browser operator escalation write contracts."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, field_validator

OperatorEscalationType = Literal[
    "voice_note",
    "complex_issue",
    "high_value_lead",
    "payment_issue",
]
OperatorEscalationPriority = Literal["low", "medium", "high"]


class OperatorEscalationCreate(BaseModel):
    reason: str
    type: OperatorEscalationType
    priority: OperatorEscalationPriority

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not 10 <= len(normalized) <= 500:
            raise ValueError("reason must contain 10 to 500 trimmed characters")
        return normalized


class OperatorEscalationActor(BaseModel):
    account_id: UUID
    display_name: str


class OperatorEscalationResponse(BaseModel):
    escalation_id: UUID
    conversation_id: UUID
    status: Literal["open"]
    reason: str
    type: OperatorEscalationType
    priority: OperatorEscalationPriority
    source: Literal["operator_browser"]
    created_at: datetime
    created_by: OperatorEscalationActor
