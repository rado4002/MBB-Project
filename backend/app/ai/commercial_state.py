"""Typed, bounded commercial continuity state stored in Conversation.context."""
from __future__ import annotations

import uuid
from enum import Enum
from typing import Annotated, Any, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)
from sqlalchemy import bindparam, func, select, update as sqlalchemy_update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation

COMMERCIAL_STATE_KEY = "commercial_state"
COMMERCIAL_STATE_SCHEMA_VERSION = 1

MAX_GOAL_LENGTH = 160
MAX_NEEDS = 6
MAX_NEED_LENGTH = 120
MAX_CONSTRAINTS = 6
MAX_CONSTRAINT_VALUE_LENGTH = 120
MAX_OPEN_QUESTIONS = 5
MAX_QUESTION_LENGTH = 160
MAX_CONCERN_DETAIL_LENGTH = 160

GoalText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_GOAL_LENGTH),
]
NeedText = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_NEED_LENGTH),
]
ConstraintValue = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_CONSTRAINT_VALUE_LENGTH,
    ),
]
QuestionText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_QUESTION_LENGTH,
    ),
]
ConcernDetail = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_CONCERN_DETAIL_LENGTH,
    ),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DecisionConstraintKind(str, Enum):
    budget = "budget"
    portability = "portability"
    timing = "timing"
    compatibility = "compatibility"
    preference = "preference"


class CommercialConcernKind(str, Enum):
    price = "price"
    quality = "quality"
    delivery = "delivery"
    comparison = "comparison"


class PurchaseIntent(str, Enum):
    none = "none"
    considering = "considering"
    ready = "ready"


class NextObjective(str, Enum):
    clarify_requirement = "clarify_requirement"
    retrieve_options = "retrieve_options"
    answer_concern = "answer_concern"
    clarify_choice = "clarify_choice"
    prepare_handoff = "prepare_handoff"


class DecisionConstraint(_StrictModel):
    kind: DecisionConstraintKind
    value: ConstraintValue


class CommercialConcern(_StrictModel):
    kind: CommercialConcernKind
    detail: ConcernDetail | None = None


class CommercialState(_StrictModel):
    schema_version: int = Field(
        default=COMMERCIAL_STATE_SCHEMA_VERSION,
        ge=COMMERCIAL_STATE_SCHEMA_VERSION,
        le=COMMERCIAL_STATE_SCHEMA_VERSION,
        strict=True,
    )
    revision: int = Field(default=0, ge=0, strict=True)
    current_goal: GoalText | None = None
    expressed_needs: list[NeedText] = Field(default_factory=list, max_length=MAX_NEEDS)
    decision_constraints: list[DecisionConstraint] = Field(
        default_factory=list,
        max_length=MAX_CONSTRAINTS,
    )
    open_questions: list[QuestionText] = Field(
        default_factory=list,
        max_length=MAX_OPEN_QUESTIONS,
    )
    current_concern: CommercialConcern | None = None
    purchase_intent: PurchaseIntent = PurchaseIntent.none
    next_objective: NextObjective | None = None

    @model_validator(mode="after")
    def unique_constraint_kinds(self) -> CommercialState:
        kinds = [constraint.kind for constraint in self.decision_constraints]
        if len(kinds) != len(set(kinds)):
            raise ValueError("decision constraint kinds must be unique")
        return self


class CommercialStateUpdate(_StrictModel):
    """Explicit partial update: omitted means no-op and null means clear."""

    current_goal: GoalText | None = None
    expressed_needs: list[NeedText] | None = Field(default=None, max_length=MAX_NEEDS)
    decision_constraints: list[DecisionConstraint] | None = Field(
        default=None,
        max_length=MAX_CONSTRAINTS,
    )
    open_questions: list[QuestionText] | None = Field(
        default=None,
        max_length=MAX_OPEN_QUESTIONS,
    )
    current_concern: CommercialConcern | None = None
    purchase_intent: PurchaseIntent | None = None
    next_objective: NextObjective | None = None

    @model_validator(mode="after")
    def unique_constraint_kinds(self) -> CommercialStateUpdate:
        if self.decision_constraints is None:
            return self
        kinds = [constraint.kind for constraint in self.decision_constraints]
        if len(kinds) != len(set(kinds)):
            raise ValueError("decision constraint kinds must be unique")
        return self


class CommercialStateError(Exception):
    """Base class for stable commercial-state failures."""


class CommercialStateConversationNotFound(CommercialStateError):
    pass


class CommercialStateInvalid(CommercialStateError):
    pass


class CommercialStateSchemaUnsupported(CommercialStateError):
    def __init__(self, schema_version: object) -> None:
        super().__init__("commercial state schema version is unsupported")
        self.schema_version = schema_version


class CommercialStateRevisionConflict(CommercialStateError):
    def __init__(self, current_revision: int) -> None:
        super().__init__("commercial state revision changed")
        self.current_revision = current_revision


class CommercialStatePersistenceError(CommercialStateError):
    pass


def commercial_state_from_context(
    context: Mapping[str, Any] | None,
) -> CommercialState | None:
    """Parse stored state without trusting malformed or unknown-version data."""
    if context is None or COMMERCIAL_STATE_KEY not in context:
        return None
    raw_state = context[COMMERCIAL_STATE_KEY]
    if not isinstance(raw_state, dict):
        raise CommercialStateInvalid("commercial state is malformed")
    schema_version = raw_state.get("schema_version")
    if type(schema_version) is int and schema_version != COMMERCIAL_STATE_SCHEMA_VERSION:
        raise CommercialStateSchemaUnsupported(schema_version)
    if type(schema_version) is not int:
        raise CommercialStateInvalid("commercial state schema version is malformed")
    try:
        return CommercialState.model_validate(raw_state)
    except ValidationError as exc:
        raise CommercialStateInvalid("commercial state is malformed") from exc


def apply_commercial_state_update(
    current: CommercialState | None,
    *,
    expected_revision: int,
    state_update: CommercialStateUpdate,
) -> CommercialState | None:
    """Apply a validated replacement/clear update with compare-and-set semantics."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    current_revision = current.revision if current is not None else 0
    if current_revision != expected_revision:
        raise CommercialStateRevisionConflict(current_revision)
    if not state_update.model_fields_set:
        return current

    base = current or CommercialState()
    candidate_data = base.model_dump(mode="json")
    collection_fields = {"expressed_needs", "decision_constraints", "open_questions"}
    for field_name in state_update.model_fields_set:
        value = getattr(state_update, field_name)
        if field_name in collection_fields and value is None:
            value = []
        elif field_name == "purchase_intent" and value is None:
            value = PurchaseIntent.none
        candidate_data[field_name] = value
    candidate_data["revision"] = current_revision
    candidate = CommercialState.model_validate(candidate_data)
    if candidate == base:
        return current

    candidate_data["revision"] = current_revision + 1
    return CommercialState.model_validate(candidate_data)


async def read_commercial_state(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> CommercialState | None:
    row = (
        await session.execute(
            select(Conversation.context).where(
                Conversation.conversation_id == conversation_id
            )
        )
    ).one_or_none()
    if row is None:
        raise CommercialStateConversationNotFound
    return commercial_state_from_context(row[0])


async def update_commercial_state(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    expected_revision: int,
    state_update: CommercialStateUpdate,
) -> CommercialState | None:
    """Lock, validate, and update only the commercial_state JSONB namespace.

    The caller owns the surrounding transaction and must commit or roll it back.
    """
    row = (
        await session.execute(
            select(Conversation.context)
            .where(Conversation.conversation_id == conversation_id)
            .with_for_update()
        )
    ).one_or_none()
    if row is None:
        raise CommercialStateConversationNotFound

    current = commercial_state_from_context(row[0])
    next_state = apply_commercial_state_update(
        current,
        expected_revision=expected_revision,
        state_update=state_update,
    )
    if next_state is current:
        return current

    namespace_patch = {
        COMMERCIAL_STATE_KEY: next_state.model_dump(mode="json")
    }
    changed = await session.execute(
        sqlalchemy_update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(
            context=Conversation.context.op("||")(
                bindparam(
                    "commercial_state_namespace_patch",
                    namespace_patch,
                    type_=JSONB,
                )
            ),
            updated_at=func.now(),
        )
    )
    if changed.rowcount != 1:
        raise CommercialStatePersistenceError(
            "commercial state namespace update was not applied"
        )
    return next_state
