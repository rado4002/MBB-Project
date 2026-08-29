"""Strict, provider-neutral identity and durable provenance for MBB AI turns."""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_turn_audit import AITurnAudit
from app.models.message import Message

MBB_AI_ACTOR_TYPE = "ai"
MBB_AI_ACTOR_ID = "mbb_ai"
MBB_AI_ACTOR_DISPLAY_NAME = "MBB AI Assistant"

MAX_EXPOSED_CAPABILITIES = 16
MAX_CAPABILITY_ACTIVITY = 16
MAX_CHANGED_FIELDS = 8

_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$"

SafeName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=_SAFE_NAME_PATTERN,
    ),
]
SafeIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=_SAFE_IDENTIFIER_PATTERN,
    ),
]


class _StrictAuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class AIActorIdentity(_StrictAuditModel):
    """The stable application-owned AI business actor, never a provider identity."""

    actor_type: Literal["ai"] = MBB_AI_ACTOR_TYPE
    actor_id: Literal["mbb_ai"] = MBB_AI_ACTOR_ID
    display_name: Literal["MBB AI Assistant"] = MBB_AI_ACTOR_DISPLAY_NAME


MBB_AI_ACTOR = AIActorIdentity()


class CapabilityAuditDecision(str, Enum):
    requested = "requested"
    executed = "executed"
    denied = "denied"


class CapabilityAuditOutcome(str, Enum):
    success = "success"
    denied = "denied"
    failed = "failed"
    not_executed = "not_executed"


class CapabilityAuditSummary(_StrictAuditModel):
    """Audit-safe projection; arguments, results, and internal objects are excluded."""

    capability_name: SafeName
    decision: CapabilityAuditDecision
    outcome: CapabilityAuditOutcome
    safe_code: SafeName | None = None

    @model_validator(mode="after")
    def decision_matches_outcome(self) -> CapabilityAuditSummary:
        allowed = {
            CapabilityAuditDecision.requested: {CapabilityAuditOutcome.not_executed},
            CapabilityAuditDecision.executed: {
                CapabilityAuditOutcome.success,
                CapabilityAuditOutcome.failed,
            },
            CapabilityAuditDecision.denied: {CapabilityAuditOutcome.denied},
        }
        if self.outcome not in allowed[self.decision]:
            raise ValueError("capability decision and outcome are inconsistent")
        if self.outcome in {
            CapabilityAuditOutcome.success,
            CapabilityAuditOutcome.not_executed,
        } and self.safe_code is not None:
            raise ValueError("safe_code is only valid for denied or failed activity")
        return self


class CommercialStateField(str, Enum):
    current_goal = "current_goal"
    expressed_needs = "expressed_needs"
    decision_constraints = "decision_constraints"
    open_questions = "open_questions"
    current_concern = "current_concern"
    purchase_intent = "purchase_intent"
    next_objective = "next_objective"
    selected_sellable_item_ids = "selected_sellable_item_ids"


class AITurnOutcome(str, Enum):
    response_generated = "response_generated"
    fallback_used = "fallback_used"
    handoff_requested = "handoff_requested"
    failed = "failed"
    no_action = "no_action"


class AITurnAuditRecord(_StrictAuditModel):
    """One finalized, minimized provenance record for a bounded AI turn."""

    turn_id: uuid.UUID
    conversation_id: uuid.UUID
    actor: AIActorIdentity = Field(default_factory=AIActorIdentity)
    source_message_id: uuid.UUID | None = None
    outbound_message_id: uuid.UUID | None = None
    policy_version: SafeIdentifier
    provider: SafeIdentifier | None = None
    model: SafeIdentifier | None = None
    exposed_capabilities: tuple[SafeName, ...] = Field(
        default=(),
        max_length=MAX_EXPOSED_CAPABILITIES,
    )
    capability_activity: tuple[CapabilityAuditSummary, ...] = Field(
        default=(),
        max_length=MAX_CAPABILITY_ACTIVITY,
    )
    commercial_state_revision_before: int | None = Field(
        default=None,
        ge=0,
    )
    commercial_state_revision_after: int | None = Field(
        default=None,
        ge=0,
    )
    commercial_state_changed_fields: tuple[CommercialStateField, ...] = Field(
        default=(),
        max_length=MAX_CHANGED_FIELDS,
    )
    outcome: AITurnOutcome
    safe_code: SafeName | None = None

    @model_validator(mode="after")
    def validate_cross_field_provenance(self) -> AITurnAuditRecord:
        if self.model is not None and self.provider is None:
            raise ValueError("model requires a provider identifier")
        if len(self.exposed_capabilities) != len(set(self.exposed_capabilities)):
            raise ValueError("exposed capability names must be unique")
        if len(self.commercial_state_changed_fields) != len(
            set(self.commercial_state_changed_fields)
        ):
            raise ValueError("commercial-state changed fields must be unique")

        before = self.commercial_state_revision_before
        after = self.commercial_state_revision_after
        if before is not None and after is not None and after < before:
            raise ValueError("commercial-state revision cannot move backwards")
        if self.commercial_state_changed_fields and (
            before is None or after is None or after <= before
        ):
            raise ValueError(
                "changed fields require an increasing before/after revision"
            )
        return self


class AIProvenanceReferenceError(ValueError):
    """An authoritative message reference is missing or belongs elsewhere."""


async def append_ai_turn_audit(
    session: AsyncSession,
    record: AITurnAuditRecord,
) -> AITurnAudit:
    """Append one finalized turn record; the caller owns the transaction."""
    await _validate_message_references(session, record)
    audit = AITurnAudit(
        turn_id=record.turn_id,
        conversation_id=record.conversation_id,
        actor_type=record.actor.actor_type,
        actor_id=record.actor.actor_id,
        actor_display_name=record.actor.display_name,
        source_message_id=record.source_message_id,
        outbound_message_id=record.outbound_message_id,
        policy_version=record.policy_version,
        provider=record.provider,
        model=record.model,
        exposed_capabilities=list(record.exposed_capabilities),
        capability_activity=[
            summary.model_dump(mode="json") for summary in record.capability_activity
        ],
        commercial_state_revision_before=record.commercial_state_revision_before,
        commercial_state_revision_after=record.commercial_state_revision_after,
        commercial_state_changed_fields=[
            item.value for item in record.commercial_state_changed_fields
        ],
        outcome=record.outcome.value,
        safe_code=record.safe_code,
    )
    session.add(audit)
    await session.flush()
    return audit


async def _validate_message_references(
    session: AsyncSession,
    record: AITurnAuditRecord,
) -> None:
    expected_directions = {
        message_id: direction
        for message_id, direction in (
            (record.source_message_id, "inbound"),
            (record.outbound_message_id, "outbound"),
        )
        if message_id is not None
    }
    if not expected_directions:
        return

    rows = (
        await session.execute(
            select(Message.message_id, Message.conversation_id, Message.direction).where(
                Message.message_id.in_(expected_directions)
            )
        )
    ).all()
    references = {
        message_id: (conversation_id, direction)
        for message_id, conversation_id, direction in rows
    }
    for message_id, expected_direction in expected_directions.items():
        actual = references.get(message_id)
        if actual is None:
            raise AIProvenanceReferenceError("authoritative message does not exist")
        conversation_id, direction = actual
        if conversation_id != record.conversation_id:
            raise AIProvenanceReferenceError(
                "authoritative message belongs to another conversation"
            )
        if direction != expected_direction:
            raise AIProvenanceReferenceError(
                "authoritative message direction does not match its audit role"
            )
