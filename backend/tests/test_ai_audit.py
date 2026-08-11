from __future__ import annotations

import inspect
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError

from app.ai.audit import (
    MAX_CAPABILITY_ACTIVITY,
    MAX_EXPOSED_CAPABILITIES,
    AIActorIdentity,
    AITurnAuditRecord,
    AITurnOutcome,
    CapabilityAuditDecision,
    CapabilityAuditOutcome,
    CapabilityAuditSummary,
    CommercialStateField,
    MBB_AI_ACTOR,
    append_ai_turn_audit,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.turn import AITurn
from app.models.ai_turn_audit import AITurnAudit


def _minimal_record(**overrides) -> AITurnAuditRecord:
    values = {
        "turn_id": AITurn(
            user_content="test",
            language="french",
            expected_ownership_version=1,
        ).turn_id,
        "conversation_id": uuid.uuid4(),
        "policy_version": AI_SYSTEM_POLICY_VERSION,
        "outcome": AITurnOutcome.failed,
        **overrides,
    }
    return AITurnAuditRecord(**values)


def test_migration_is_linear_additive_seed_free_and_reversible() -> None:
    revision = "f6a7b8c9d0e1"
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_current_head() == "b8c9d0e1f2a3"
    assert script.get_revision("b8c9d0e1f2a3").down_revision == "a7b8c9d0e1f2"
    assert script.get_revision("a7b8c9d0e1f2").down_revision == revision
    assert script.get_revision(revision).down_revision == "e5f6a7b8c9d0"

    source = (
        Path("alembic/versions") / "f6a7b8c9d0e1_add_ai_turn_audits.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    assert 'op.create_table(\n        "ai_turn_audits"' in source
    assert 'op.drop_table("ai_turn_audits"' in source
    assert "op.bulk_insert" not in lowered
    assert "insert into" not in lowered
    assert "op.add_column" not in lowered
    assert "op.alter_column" not in lowered


def test_stable_actor_is_application_owned_and_not_an_operator_account() -> None:
    assert MBB_AI_ACTOR == AIActorIdentity(
        actor_type="ai",
        actor_id="mbb_ai",
        display_name="MBB AI Assistant",
    )
    assert "provider" not in AIActorIdentity.model_fields
    assert "model" not in AIActorIdentity.model_fields
    assert "operator_account_id" not in AIActorIdentity.model_fields
    assert "actor_account_id" not in AITurnAudit.__table__.columns
    assert not any(
        foreign_key.target_fullname == "mbb.operator_accounts.account_id"
        for foreign_key in AITurnAudit.__table__.foreign_keys
    )


def test_turn_identity_is_unique_opaque_and_not_caller_overridable() -> None:
    first = AITurn(
        user_content="first", language="french", expected_ownership_version=1
    )
    second = AITurn(
        user_content="second", language="lingala", expected_ownership_version=2
    )

    assert isinstance(first.turn_id, uuid.UUID)
    assert first.turn_id != second.turn_id
    assert "turn_id" not in inspect.signature(AITurn).parameters
    with pytest.raises(TypeError):
        AITurn(  # type: ignore[call-arg]
            user_content="provider content",
            language="french",
            expected_ownership_version=1,
            turn_id=uuid.uuid4(),
        )


def test_minimal_and_provider_metadata_provenance_validate_strictly() -> None:
    minimal = _minimal_record()
    executed = _minimal_record(
        provider="deepseek",
        model="deepseek-chat",
        exposed_capabilities=("search_products",),
        capability_activity=(
            CapabilityAuditSummary(
                capability_name="search_products",
                decision=CapabilityAuditDecision.executed,
                outcome=CapabilityAuditOutcome.success,
            ),
        ),
    )

    assert minimal.actor is not None and minimal.actor.actor_id == "mbb_ai"
    assert executed.actor.actor_id == minimal.actor.actor_id
    assert executed.provider == "deepseek"
    assert executed.model == "deepseek-chat"

    with pytest.raises(ValidationError):
        _minimal_record(model="provider-cannot-be-inferred")
    with pytest.raises(ValidationError):
        AITurnAuditRecord.model_validate(
            {
                "turn_id": str(uuid.uuid4()),
                "conversation_id": str(uuid.uuid4()),
                "policy_version": AI_SYSTEM_POLICY_VERSION,
                "outcome": "failed",
            },
            strict=True,
        )


@pytest.mark.parametrize(
    "forbidden_field",
    (
        "prompt",
        "system_prompt",
        "completion",
        "message_body",
        "internal_note",
        "tool_arguments",
        "tool_result",
        "chain_of_thought",
        "price",
        "stock",
        "order",
        "payment",
        "secret",
        "token",
    ),
)
def test_unknown_content_business_and_secret_fields_are_rejected(
    forbidden_field: str,
) -> None:
    values = _minimal_record().model_dump()
    values[forbidden_field] = "must not persist"

    with pytest.raises(ValidationError):
        AITurnAuditRecord.model_validate(values)


def test_text_and_list_bounds_reject_excessive_values() -> None:
    with pytest.raises(ValidationError):
        _minimal_record(provider="p" * 101)
    with pytest.raises(ValidationError):
        _minimal_record(safe_code="x" * 65)
    with pytest.raises(ValidationError):
        _minimal_record(
            exposed_capabilities=tuple(
                f"capability_{index}" for index in range(MAX_EXPOSED_CAPABILITIES + 1)
            )
        )

    summary = CapabilityAuditSummary(
        capability_name="search_products",
        decision=CapabilityAuditDecision.executed,
        outcome=CapabilityAuditOutcome.success,
    )
    with pytest.raises(ValidationError):
        _minimal_record(
            capability_activity=(summary,) * (MAX_CAPABILITY_ACTIVITY + 1)
        )


def test_capability_summary_is_bounded_and_cannot_hold_raw_payloads() -> None:
    denied = CapabilityAuditSummary(
        capability_name="take_payment",
        decision=CapabilityAuditDecision.denied,
        outcome=CapabilityAuditOutcome.denied,
        safe_code="tool_not_allowed",
    )
    assert denied.model_dump(mode="json") == {
        "capability_name": "take_payment",
        "decision": "denied",
        "outcome": "denied",
        "safe_code": "tool_not_allowed",
    }

    for raw_field in ("arguments", "result", "orm_object", "provider_payload"):
        values = denied.model_dump()
        values[raw_field] = {"raw": object()}
        with pytest.raises(ValidationError):
            CapabilityAuditSummary.model_validate(values)
    with pytest.raises(ValidationError):
        CapabilityAuditSummary(
            capability_name="search_products",
            decision=CapabilityAuditDecision.executed,
            outcome=CapabilityAuditOutcome.success,
            safe_code="should_not_exist",
        )


def test_handoff_capability_fits_existing_provenance_contract() -> None:
    summary = CapabilityAuditSummary(
        capability_name="request_human_handoff",
        decision=CapabilityAuditDecision.executed,
        outcome=CapabilityAuditOutcome.success,
    )
    record = _minimal_record(
        outcome=AITurnOutcome.handoff_requested,
        exposed_capabilities=("request_human_handoff",),
        capability_activity=(summary,),
    )

    assert record.outcome == AITurnOutcome.handoff_requested
    assert record.capability_activity == (summary,)


def test_commercial_state_uses_only_revision_and_bounded_field_references() -> None:
    record = _minimal_record(
        commercial_state_revision_before=4,
        commercial_state_revision_after=5,
        commercial_state_changed_fields=(CommercialStateField.purchase_intent,),
    )

    assert record.commercial_state_revision_before == 4
    assert record.commercial_state_revision_after == 5
    assert set(AITurnAuditRecord.model_fields).isdisjoint(
        {"commercial_state", "state_before", "state_after"}
    )
    with pytest.raises(ValidationError):
        _minimal_record(
            commercial_state_revision_before=5,
            commercial_state_revision_after=4,
        )
    with pytest.raises(ValidationError):
        _minimal_record(
            commercial_state_revision_before=4,
            commercial_state_revision_after=4,
            commercial_state_changed_fields=(CommercialStateField.current_goal,),
        )


def test_contract_and_table_exclude_customer_content_and_business_truth() -> None:
    prohibited = {
        "prompt",
        "system_prompt",
        "completion",
        "message_body",
        "content",
        "internal_note",
        "tool_arguments",
        "tool_results",
        "chain_of_thought",
        "customer",
        "product",
        "price",
        "stock",
        "order",
        "payment",
        "secret",
        "token",
        "latency",
        "cost",
        "token_count",
    }
    assert prohibited.isdisjoint(AITurnAuditRecord.model_fields)
    assert prohibited.isdisjoint(AITurnAudit.__table__.columns.keys())


class _RecordingSession:
    def __init__(self) -> None:
        self.added = []
        self.flushed = False

    def add(self, value) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        self.flushed = True


@pytest.mark.asyncio
async def test_append_builds_one_finalized_row_without_a_mutation_api() -> None:
    session = _RecordingSession()
    record = _minimal_record(
        provider="deepseek",
        model="deepseek-chat",
        outcome=AITurnOutcome.no_action,
    )

    audit = await append_ai_turn_audit(session, record)  # type: ignore[arg-type]

    assert session.added == [audit]
    assert session.flushed
    assert audit.turn_id == record.turn_id
    assert audit.actor_id == "mbb_ai"
    assert audit.provider == "deepseek"
    assert audit.model == "deepseek-chat"
    assert not hasattr(audit, "operator_account_id")


def test_production_turn_path_is_not_activated_by_the_foundation() -> None:
    from app.tasks import m1

    source = inspect.getsource(m1._process)
    assert "append_ai_turn_audit" not in source
    assert "AITurnAuditRecord" not in source
