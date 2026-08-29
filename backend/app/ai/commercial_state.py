"""Typed, bounded commercial continuity state stored in Conversation.context."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Annotated, Any, Literal, Mapping

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import bindparam, func, select, update as sqlalchemy_update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import SellableItem
from app.models.conversation import Conversation

COMMERCIAL_STATE_KEY = "commercial_state"
COMMERCIAL_STATE_SCHEMA_VERSION = 2
LEGACY_COMMERCIAL_STATE_SCHEMA_VERSION = 1
COMMERCIAL_STATE_FINALIZER = "propose_commercial_state_update"

MAX_GOAL_LENGTH = 160
MAX_NEEDS = 6
MAX_NEED_LENGTH = 120
MAX_CONSTRAINTS = 6
MAX_CONSTRAINT_VALUE_LENGTH = 120
MAX_OPEN_QUESTIONS = 5
MAX_QUESTION_LENGTH = 160
MAX_CONCERN_DETAIL_LENGTH = 160
MAX_SELECTED_SELLABLE_ITEMS = 3
MAX_RESPONSE_TEXT_LENGTH = 2_000

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
ResponseText = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=MAX_RESPONSE_TEXT_LENGTH,
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
    """Bounded intent memory; ready remains terminal and server-controlled."""

    none = "none"
    considering = "considering"
    ready = "ready"


class NextObjective(str, Enum):
    clarify_requirement = "clarify_requirement"
    retrieve_options = "retrieve_options"
    answer_concern = "answer_concern"
    clarify_choice = "clarify_choice"
    prepare_handoff = "prepare_handoff"
    human_commercial_continuation = "human_commercial_continuation"


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
    selected_sellable_item_ids: list[uuid.UUID] = Field(
        default_factory=list,
        max_length=MAX_SELECTED_SELLABLE_ITEMS,
    )
    updated_at: datetime | None = None

    @model_validator(mode="after")
    def structurally_consistent(self) -> CommercialState:
        _require_unique_constraint_kinds(self.decision_constraints)
        if len(self.selected_sellable_item_ids) != len(
            set(self.selected_sellable_item_ids)
        ):
            raise ValueError("selected Sellable Item IDs must be unique")
        if self.updated_at is not None:
            if self.updated_at.tzinfo is None or self.updated_at.utcoffset() is None:
                raise ValueError("commercial-state updated_at must be timezone-aware")
            if self.updated_at.utcoffset().total_seconds() != 0:
                raise ValueError("commercial-state updated_at must be UTC")
        return self


class _LegacyCommercialState(_StrictModel):
    """Exact v1 read contract, normalized in memory without rewriting JSONB."""

    schema_version: int = Field(default=1, ge=1, le=1, strict=True)
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
    def unique_constraint_kinds(self) -> _LegacyCommercialState:
        _require_unique_constraint_kinds(self.decision_constraints)
        return self


class CommercialStateUpdate(_StrictModel):
    """Model-safe partial update: omitted means unchanged and null means clear."""

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
    purchase_intent: Literal[PurchaseIntent.none, PurchaseIntent.considering] | None = (
        None
    )
    next_objective: NextObjective | None = None
    selected_sellable_item_ids: list[uuid.UUID] | None = Field(
        default=None,
        max_length=MAX_SELECTED_SELLABLE_ITEMS,
    )

    @field_validator("selected_sellable_item_ids", mode="before")
    @classmethod
    def canonical_selected_ids(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, list):
            raise ValueError("selected Sellable Item IDs must be a list")
        for item in value:
            if not isinstance(item, str):
                raise ValueError("selected Sellable Item IDs must be canonical UUIDs")
            try:
                parsed = uuid.UUID(item)
            except (ValueError, AttributeError) as exc:
                raise ValueError(
                    "selected Sellable Item IDs must be canonical UUIDs"
                ) from exc
            if str(parsed) != item:
                raise ValueError("selected Sellable Item IDs must be canonical UUIDs")
        return value

    @model_validator(mode="after")
    def structurally_consistent(self) -> CommercialStateUpdate:
        if self.decision_constraints is not None:
            _require_unique_constraint_kinds(self.decision_constraints)
        if self.selected_sellable_item_ids is not None and len(
            self.selected_sellable_item_ids
        ) != len(set(self.selected_sellable_item_ids)):
            raise ValueError("selected Sellable Item IDs must be unique")
        if (
            "next_objective" in self.model_fields_set
            and self.next_objective is NextObjective.human_commercial_continuation
        ):
            raise ValueError(
                "post-handoff objective is controlled by the terminal transaction"
            )
        return self


class CommercialStateProposal(_StrictModel):
    """Only safe final model output: response text plus a bounded state patch."""

    response_text: ResponseText
    state_update: CommercialStateUpdate


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


class CommercialStateSelectedItemInvalid(CommercialStateError):
    pass


class CommercialStatePersistenceError(CommercialStateError):
    pass


def _require_unique_constraint_kinds(
    constraints: list[DecisionConstraint],
) -> None:
    kinds = [constraint.kind for constraint in constraints]
    if len(kinds) != len(set(kinds)):
        raise ValueError("decision constraint kinds must be unique")


def commercial_state_from_context(
    context: Mapping[str, Any] | None,
) -> CommercialState | None:
    """Parse v2 or normalize v1 safely without inventing missing metadata."""
    if context is None or COMMERCIAL_STATE_KEY not in context:
        return None
    raw_state = context[COMMERCIAL_STATE_KEY]
    if not isinstance(raw_state, dict):
        raise CommercialStateInvalid("commercial state is malformed")
    schema_version = raw_state.get("schema_version")
    if type(schema_version) is not int:
        raise CommercialStateInvalid("commercial state schema version is malformed")
    try:
        if schema_version == COMMERCIAL_STATE_SCHEMA_VERSION:
            return CommercialState.model_validate(raw_state)
        if schema_version == LEGACY_COMMERCIAL_STATE_SCHEMA_VERSION:
            legacy = _LegacyCommercialState.model_validate(raw_state)
            values = legacy.model_dump(mode="json", exclude={"schema_version"})
            return CommercialState.model_validate(values)
    except ValidationError as exc:
        raise CommercialStateInvalid("commercial state is malformed") from exc
    raise CommercialStateSchemaUnsupported(schema_version)


def commercial_state_projection(state: CommercialState | None) -> dict[str, Any]:
    """Return compact conversational fields; historical intent is not fresh evidence."""
    if state is None:
        return {}
    projection = state.model_dump(
        mode="json",
        exclude={"schema_version", "revision", "updated_at", "purchase_intent"},
    )
    return {
        field_name: value
        for field_name, value in projection.items()
        if value not in (None, [], {})
    }


def apply_commercial_state_update(
    current: CommercialState | None,
    *,
    expected_revision: int,
    state_update: CommercialStateUpdate,
    changed_at: datetime | None = None,
) -> CommercialState | None:
    """Apply bounded replacement/clear semantics with revision compare-and-set."""
    if type(expected_revision) is not int or expected_revision < 0:
        raise ValueError("expected_revision must be a non-negative integer")
    current_revision = current.revision if current is not None else 0
    if current_revision != expected_revision:
        raise CommercialStateRevisionConflict(current_revision)
    if not state_update.model_fields_set:
        return current

    base = current or CommercialState()
    candidate_data = base.model_dump(mode="json")
    collection_fields = {
        "expressed_needs",
        "decision_constraints",
        "open_questions",
        "selected_sellable_item_ids",
    }
    goal_changed = (
        "current_goal" in state_update.model_fields_set
        and state_update.current_goal != base.current_goal
        and base.current_goal is not None
    )
    if goal_changed:
        for field_name in (
            "expressed_needs",
            "decision_constraints",
            "open_questions",
            "current_concern",
            "selected_sellable_item_ids",
            "next_objective",
        ):
            if field_name not in state_update.model_fields_set:
                candidate_data[field_name] = [] if field_name in collection_fields else None

    for field_name in state_update.model_fields_set:
        value = getattr(state_update, field_name)
        if field_name in collection_fields and value is None:
            value = []
        if field_name == "purchase_intent" and value is None:
            value = PurchaseIntent.none
        candidate_data[field_name] = value

    candidate_data.update(
        schema_version=COMMERCIAL_STATE_SCHEMA_VERSION,
        revision=current_revision,
        updated_at=base.updated_at,
    )
    candidate = CommercialState.model_validate(candidate_data)
    if _semantic_state(candidate) == _semantic_state(base):
        return current

    timestamp = changed_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError("changed_at must be timezone-aware")
    candidate_data["revision"] = current_revision + 1
    candidate_data["updated_at"] = timestamp.astimezone(timezone.utc)
    return CommercialState.model_validate(candidate_data)


def _semantic_state(state: CommercialState) -> dict[str, Any]:
    return state.model_dump(
        mode="json",
        exclude={"schema_version", "revision", "updated_at"},
    )


def commercial_state_changed_fields(
    before: CommercialState | None,
    after: CommercialState | None,
) -> tuple[str, ...]:
    """Return bounded customer-memory field names whose semantic values changed."""
    before_state = before or CommercialState()
    after_state = after or CommercialState()
    fields = (
        "current_goal",
        "expressed_needs",
        "decision_constraints",
        "open_questions",
        "current_concern",
        "purchase_intent",
        "next_objective",
        "selected_sellable_item_ids",
    )
    return tuple(
        field_name
        for field_name in fields
        if getattr(before_state, field_name) != getattr(after_state, field_name)
    )


async def update_commercial_state_for_handoff_from_locked_snapshot(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    current: CommercialState | None,
    expected_revision: int,
    purchase_intent: Literal[PurchaseIntent.considering, PurchaseIntent.ready]
    | None,
    selected_sellable_item_id: uuid.UUID | None,
) -> CommercialState:
    """Persist the server-controlled post-handoff state from a locked snapshot."""
    current_revision = current.revision if current is not None else 0
    if current_revision != expected_revision:
        raise CommercialStateRevisionConflict(current_revision)
    if purchase_intent is PurchaseIntent.ready and selected_sellable_item_id is None:
        raise CommercialStateSelectedItemInvalid(
            "ready purchase intent requires exactly one selected Sellable Item"
        )

    base = current or CommercialState()
    candidate_data = base.model_dump(mode="json")
    if purchase_intent is not None:
        candidate_data["purchase_intent"] = purchase_intent
    if selected_sellable_item_id is not None:
        candidate_data["selected_sellable_item_ids"] = [selected_sellable_item_id]
    if purchase_intent is PurchaseIntent.ready and len(
        candidate_data["selected_sellable_item_ids"]
    ) != 1:
        raise CommercialStateSelectedItemInvalid(
            "ready purchase intent requires exactly one selected Sellable Item"
        )
    candidate_data.update(
        schema_version=COMMERCIAL_STATE_SCHEMA_VERSION,
        revision=current_revision,
        updated_at=base.updated_at,
        next_objective=NextObjective.human_commercial_continuation,
    )
    candidate = CommercialState.model_validate(candidate_data)
    if _semantic_state(candidate) == _semantic_state(base):
        return base

    candidate_data.update(
        revision=current_revision + 1,
        updated_at=datetime.now(timezone.utc),
    )
    next_state = CommercialState.model_validate(candidate_data)
    await _validate_new_selected_item_ids(session, current, next_state)
    namespace_patch = {COMMERCIAL_STATE_KEY: next_state.model_dump(mode="json")}
    changed = await session.execute(
        sqlalchemy_update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(
            context=Conversation.context.op("||")(
                bindparam(
                    "terminal_commercial_state_namespace_patch",
                    namespace_patch,
                    type_=JSONB,
                )
            ),
            updated_at=func.now(),
        )
    )
    if changed.rowcount != 1:
        raise CommercialStatePersistenceError(
            "terminal commercial state namespace update was not applied"
        )
    return next_state


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
    """Lock, validate IDs, and update only the commercial-state JSONB namespace."""
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
    return await update_commercial_state_from_locked_snapshot(
        session,
        conversation_id=conversation_id,
        current=current,
        expected_revision=expected_revision,
        state_update=state_update,
    )


async def update_commercial_state_from_locked_snapshot(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    current: CommercialState | None,
    expected_revision: int,
    state_update: CommercialStateUpdate,
) -> CommercialState | None:
    """Update from a snapshot read while the caller holds the Conversation lock."""
    next_state = apply_commercial_state_update(
        current,
        expected_revision=expected_revision,
        state_update=state_update,
    )
    if next_state is current:
        return current

    await _validate_new_selected_item_ids(session, current, next_state)
    assert next_state is not None
    namespace_patch = {COMMERCIAL_STATE_KEY: next_state.model_dump(mode="json")}
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


async def _validate_new_selected_item_ids(
    session: AsyncSession,
    current: CommercialState | None,
    next_state: CommercialState,
) -> None:
    previous_ids = set(current.selected_sellable_item_ids) if current else set()
    proposed_ids = set(next_state.selected_sellable_item_ids)
    new_ids = proposed_ids - previous_ids
    if not new_ids:
        return
    existing_ids = set(
        await session.scalars(
            select(SellableItem.sellable_item_id).where(
                SellableItem.sellable_item_id.in_(new_ids)
            )
        )
    )
    if existing_ids != new_ids:
        raise CommercialStateSelectedItemInvalid(
            "proposed Sellable Item ID does not exist"
        )
