from __future__ import annotations

import inspect
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.inbound_turn_lifecycle import InboundTurnLifecycle
from app.modules.m1_gateway import turn_recovery


REVISION = "b4c5d6e7f8a9"


def test_migration_is_linear_additive_content_free_and_reversible() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_current_head() == REVISION
    assert script.get_revision(REVISION).down_revision == "a3b4c5d6e7f8"

    source = (
        Path("alembic/versions")
        / "b4c5d6e7f8a9_add_inbound_turn_lifecycles.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    assert 'op.create_table(\n        "inbound_turn_lifecycles"' in source
    assert 'op.drop_table("inbound_turn_lifecycles"' in source
    assert "op.bulk_insert" not in lowered
    assert "insert into" not in lowered
    for forbidden in (
        "prompt",
        "customer_text",
        "reasoning",
        "tool_arguments",
        "tool_results",
        "payload",
        "exception",
    ):
        assert forbidden not in lowered


def test_lifecycle_schema_contains_only_bounded_state_and_identifiers() -> None:
    assert set(InboundTurnLifecycle.__table__.columns.keys()) == {
        "source_message_id",
        "conversation_id",
        "ownership_version",
        "state",
        "outcome_type",
        "outbound_message_id",
        "disposition_code",
        "attempt_id",
        "claim_expires_at",
        "created_at",
        "updated_at",
    }
    assert InboundTurnLifecycle.__table__.c.state.type.length == 24
    assert InboundTurnLifecycle.__table__.c.outcome_type.type.length == 24
    assert InboundTurnLifecycle.__table__.c.disposition_code.type.length == 64
    assert InboundTurnLifecycle.__table__.c.attempt_id.type.length == 64


def test_recovery_is_an_explicit_m1_lifecycle_not_a_workflow_engine() -> None:
    source = inspect.getsource(turn_recovery)
    assert "class TurnClaim" in source
    assert "class RecoveryOutbound" in source
    assert "workflow" not in source.lower()
    assert "celery_app" not in source
    assert "redis" not in source.lower()
