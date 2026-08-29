from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

from app.ai.commercial_state import (
    COMMERCIAL_STATE_SCHEMA_VERSION,
    MAX_GOAL_LENGTH,
    CommercialConcern,
    CommercialConcernKind,
    CommercialState,
    CommercialStateInvalid,
    CommercialStateProposal,
    CommercialStateRevisionConflict,
    CommercialStateSchemaUnsupported,
    CommercialStateUpdate,
    DecisionConstraint,
    DecisionConstraintKind,
    NextObjective,
    PurchaseIntent,
    apply_commercial_state_update,
    commercial_state_projection,
    commercial_state_from_context,
    update_commercial_state,
)
from app.schemas.conversations import ConversationContextUpdate


def test_minimal_state_is_typed_versioned_and_strict():
    state = CommercialState()

    assert state.schema_version == COMMERCIAL_STATE_SCHEMA_VERSION
    assert state.revision == 0
    assert state.purchase_intent is PurchaseIntent.none
    with pytest.raises(ValidationError):
        CommercialState.model_validate({"schema_version": 1, "revision": 0, "price": 20})


@pytest.mark.parametrize("field", ["price", "stock", "order_status", "payment_status"])
def test_authoritative_business_fields_are_rejected(field):
    with pytest.raises(ValidationError):
        CommercialStateUpdate.model_validate({field: "not allowed"})


def test_generic_context_contract_rejects_reserved_commercial_state_namespace():
    with pytest.raises(ValidationError, match="commercial_state is reserved"):
        ConversationContextUpdate(
            context={"commercial_state": {"schema_version": 1, "revision": 0}}
        )


def test_schema_and_data_bounds_reject_unsupported_or_excessive_state():
    with pytest.raises(CommercialStateSchemaUnsupported):
        commercial_state_from_context(
            {"commercial_state": {"schema_version": 3, "revision": 0}}
        )
    with pytest.raises(ValidationError):
        CommercialState(current_goal="x" * (MAX_GOAL_LENGTH + 1))
    with pytest.raises(ValidationError):
        CommercialState(expressed_needs=[f"need-{index}" for index in range(7)])
    with pytest.raises(ValidationError):
        CommercialState(
            decision_constraints=[
                DecisionConstraint(kind="budget", value="20 USD"),
                DecisionConstraint(kind="budget", value="35 USD"),
            ]
        )


def test_missing_valid_and_malformed_stored_state_handling():
    assert commercial_state_from_context({"existing_key": True}) is None
    expected = CommercialState(current_goal="find a travel-friendly option")
    stored = {
        "existing_key": True,
        "commercial_state": expected.model_dump(mode="json"),
    }
    assert commercial_state_from_context(stored) == expected
    with pytest.raises(CommercialStateInvalid):
        commercial_state_from_context({"commercial_state": "not-an-object"})
    with pytest.raises(CommercialStateInvalid):
        commercial_state_from_context(
            {"commercial_state": {"schema_version": 1, "revision": "bad"}}
        )


def test_updates_replace_clear_and_reverse_customer_position():
    state = apply_commercial_state_update(
        None,
        expected_revision=0,
        state_update=CommercialStateUpdate(
            current_goal="find something portable",
            expressed_needs=["easy to carry"],
            decision_constraints=[
                DecisionConstraint(kind=DecisionConstraintKind.budget, value="around $20")
            ],
            open_questions=["Which devices must it support?"],
            current_concern=CommercialConcern(
                kind=CommercialConcernKind.price,
                detail="Customer considers the first option expensive",
            ),
            next_objective=NextObjective.clarify_requirement,
        ),
    )
    assert state is not None
    assert state.revision == 1

    state = apply_commercial_state_update(
        state,
        expected_revision=1,
        state_update=CommercialStateUpdate(
            expressed_needs=["works with two devices"],
            decision_constraints=[
                DecisionConstraint(kind=DecisionConstraintKind.budget, value="around $35")
            ],
            open_questions=None,
            next_objective=NextObjective.clarify_choice,
        ),
    )
    assert state is not None
    assert state.revision == 2
    assert state.expressed_needs == ["works with two devices"]
    assert [item.value for item in state.decision_constraints] == ["around $35"]
    assert state.open_questions == []
    assert state.purchase_intent is PurchaseIntent.none

    state = apply_commercial_state_update(
        state,
        expected_revision=2,
        state_update=CommercialStateUpdate(
            expressed_needs=None,
            decision_constraints=None,
            current_concern=None,
            next_objective=NextObjective.answer_concern,
        ),
    )
    assert state is not None
    assert state.revision == 3
    assert state.expressed_needs == []
    assert state.decision_constraints == []
    assert state.current_concern is None
    assert state.purchase_intent is PurchaseIntent.none
    assert state.next_objective is NextObjective.answer_concern


def test_no_op_does_not_initialize_or_increment_revision():
    assert (
        apply_commercial_state_update(
            None,
            expected_revision=0,
            state_update=CommercialStateUpdate(),
        )
        is None
    )
    current = CommercialState(revision=4, current_goal="compare suitable options")
    unchanged = apply_commercial_state_update(
        current,
        expected_revision=4,
        state_update=CommercialStateUpdate(current_goal="compare suitable options"),
    )
    assert unchanged is current


def test_stale_writer_cannot_overwrite_newer_state():
    first = apply_commercial_state_update(
        None,
        expected_revision=0,
        state_update=CommercialStateUpdate(current_goal="find a portable option"),
    )
    assert first is not None
    newer = apply_commercial_state_update(
        first,
        expected_revision=1,
        state_update=CommercialStateUpdate(expressed_needs=["easy to carry"]),
    )
    assert newer is not None

    with pytest.raises(CommercialStateRevisionConflict) as captured:
        apply_commercial_state_update(
            newer,
            expected_revision=1,
            state_update=CommercialStateUpdate(current_goal="stale overwrite"),
        )
    assert captured.value.current_revision == 2
    assert newer.current_goal == "find a portable option"


def test_legacy_v1_normalizes_without_exposing_or_changing_purchase_intent():
    legacy = {
        "schema_version": 1,
        "revision": 4,
        "current_goal": "find an air fryer",
        "expressed_needs": ["serves five people"],
        "decision_constraints": [{"kind": "budget", "value": "maximum $70"}],
        "open_questions": [],
        "current_concern": None,
        "purchase_intent": "ready",
        "next_objective": "retrieve_options",
    }

    state = commercial_state_from_context({"commercial_state": legacy})

    assert state is not None
    assert state.schema_version == 2
    assert state.revision == 4
    assert state.updated_at is None
    assert state.purchase_intent is PurchaseIntent.ready
    projection = commercial_state_projection(state)
    assert "purchase_intent" not in projection
    assert "schema_version" not in projection
    assert "revision" not in projection
    assert "updated_at" not in projection


def test_proposal_rejects_unknown_business_facts_and_purchase_intent():
    for update in (
        {"price": 55},
        {"stock": "available"},
        {"purchase_intent": "ready"},
        {"confidence": 0.9},
    ):
        with pytest.raises(ValidationError):
            CommercialStateProposal.model_validate(
                {"response_text": "Safe response", "state_update": update}
            )


def test_selected_ids_are_canonical_unique_and_limited_to_three():
    ids = [str(uuid.uuid4()) for _ in range(4)]
    valid = CommercialStateUpdate(selected_sellable_item_ids=ids[:3])
    assert valid.selected_sellable_item_ids == [uuid.UUID(item) for item in ids[:3]]
    with pytest.raises(ValidationError):
        CommercialStateUpdate(selected_sellable_item_ids=ids)
    with pytest.raises(ValidationError):
        CommercialStateUpdate(selected_sellable_item_ids=[ids[0], ids[0]])
    with pytest.raises(ValidationError):
        CommercialStateUpdate(selected_sellable_item_ids=[ids[0].upper()])


def test_budget_replacement_additive_need_vague_concern_and_selection_removal():
    selected_id = uuid.uuid4()
    before = CommercialState(
        revision=5,
        current_goal="find an air fryer",
        expressed_needs=["serves five people"],
        decision_constraints=[DecisionConstraint(kind="budget", value="maximum $70")],
        selected_sellable_item_ids=[selected_id],
    )
    changed = apply_commercial_state_update(
        before,
        expected_revision=5,
        state_update=CommercialStateUpdate(
            expressed_needs=["serves five people", "easy to clean"],
            decision_constraints=[
                DecisionConstraint(kind="budget", value="maximum $50")
            ],
            selected_sellable_item_ids=[],
            current_concern=CommercialConcern(
                kind="price",
                detail="$55 is a little expensive",
            ),
        ),
    )
    assert changed is not None
    assert [item.value for item in changed.decision_constraints] == ["maximum $50"]
    assert changed.expressed_needs == ["serves five people", "easy to clean"]
    assert changed.selected_sellable_item_ids == []
    assert changed.current_concern is not None
    assert changed.current_concern.kind is CommercialConcernKind.price


def test_selection_replacement_keeps_only_the_latest_explicit_choice():
    old_id = uuid.uuid4()
    new_id = uuid.uuid4()
    before = CommercialState(
        revision=2,
        current_goal="find an air fryer",
        selected_sellable_item_ids=[old_id],
    )
    after = apply_commercial_state_update(
        before,
        expected_revision=2,
        state_update=CommercialStateUpdate(
            selected_sellable_item_ids=[str(new_id)]
        ),
    )
    assert after is not None
    assert after.selected_sellable_item_ids == [new_id]


def test_unrelated_goal_change_clears_incompatible_journey_fields():
    before = CommercialState(
        revision=2,
        current_goal="find an air fryer",
        expressed_needs=["8L capacity"],
        decision_constraints=[DecisionConstraint(kind="budget", value="$60")],
        open_questions=["Which fryer capacity?"],
        current_concern=CommercialConcern(kind="price"),
        next_objective=NextObjective.clarify_choice,
        selected_sellable_item_ids=[uuid.uuid4()],
    )
    after = apply_commercial_state_update(
        before,
        expected_revision=2,
        state_update=CommercialStateUpdate(current_goal="find a smart lock"),
    )
    assert after is not None
    assert after.current_goal == "find a smart lock"
    assert after.expressed_needs == []
    assert after.decision_constraints == []
    assert after.open_questions == []
    assert after.current_concern is None
    assert after.next_objective is None
    assert after.selected_sellable_item_ids == []


def test_no_op_preserves_updated_at_and_meaningful_change_sets_utc_time():
    timestamp = datetime(2026, 8, 29, 1, 2, tzinfo=timezone.utc)
    current = CommercialState(
        revision=3,
        current_goal="find an air fryer",
        updated_at=timestamp,
    )
    no_op = apply_commercial_state_update(
        current,
        expected_revision=3,
        state_update=CommercialStateUpdate(current_goal="find an air fryer"),
    )
    assert no_op is current
    changed = apply_commercial_state_update(
        current,
        expected_revision=3,
        state_update=CommercialStateUpdate(expressed_needs=["easy to clean"]),
        changed_at=datetime(2026, 8, 29, 2, 3, tzinfo=timezone.utc),
    )
    assert changed is not None
    assert changed.revision == 4
    assert changed.updated_at == datetime(2026, 8, 29, 2, 3, tzinfo=timezone.utc)


class _Result:
    def __init__(self, row=None, *, rowcount=None):
        self._row = row
        self.rowcount = rowcount

    def one_or_none(self):
        return self._row


class _RecordingSession:
    def __init__(self, context):
        self._results = iter((_Result((context,)), _Result(rowcount=1)))
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return next(self._results)


@pytest.mark.asyncio
async def test_persistence_uses_locked_namespace_only_jsonb_update():
    existing_context = {
        "existing_key": {"preserve": True},
        "qualification_state": {"step": "q2_location"},
    }
    session = _RecordingSession(existing_context)

    state = await update_commercial_state(
        session,
        conversation_id=uuid.uuid4(),
        expected_revision=0,
        state_update=CommercialStateUpdate(current_goal="understand travel need"),
    )

    assert state is not None and state.revision == 1
    select_sql = str(
        session.statements[0].compile(dialect=postgresql.dialect())
    ).upper()
    update_compiled = session.statements[1].compile(dialect=postgresql.dialect())
    update_sql = str(update_compiled)
    assert "FOR UPDATE" in select_sql
    assert "conversations.context ||" in update_sql
    patch = update_compiled.params["commercial_state_namespace_patch"]
    assert set(patch) == {"commercial_state"}
    simulated_result = existing_context | patch
    assert simulated_result["existing_key"] == {"preserve": True}
    assert simulated_result["qualification_state"] == {"step": "q2_location"}
    assert simulated_result["commercial_state"]["revision"] == 1


@pytest.mark.asyncio
async def test_malformed_state_is_rejected_before_persistence():
    session = _RecordingSession(
        {"commercial_state": {"schema_version": 1, "revision": "bad"}}
    )

    with pytest.raises(CommercialStateInvalid):
        await update_commercial_state(
            session,
            conversation_id=uuid.uuid4(),
            expected_revision=0,
            state_update=CommercialStateUpdate(current_goal="must not overwrite"),
        )
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_generic_context_endpoint_uses_atomic_jsonb_merge():
    from app.api.v1.conversations import update_conversation_context

    conversation_id = uuid.uuid4()
    returned_context = {"existing_key": True, "new_key": "value"}
    row = SimpleNamespace(
        conversation_id=conversation_id,
        context=returned_context,
        updated_at=datetime.now(timezone.utc),
    )

    class _EndpointResult:
        def one_or_none(self):
            return row

    class _EndpointSession:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _EndpointResult()

    session = _EndpointSession()
    response = await update_conversation_context(
        conversation_id=conversation_id,
        body=ConversationContextUpdate(context={"new_key": "value"}),
        db=session,
        idempotency_key="test-key",
    )

    compiled = session.statement.compile(dialect=postgresql.dialect())
    assert "conversations.context ||" in str(compiled)
    assert compiled.params["conversation_context_patch"] == {"new_key": "value"}
    assert response.context == returned_context
