from __future__ import annotations

import inspect
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.models.inbound_turn_lifecycle import InboundTurnLifecycle
from app.modules.m1_gateway import turn_recovery


REVISION = "b4c5d6e7f8a9"


class _LegacySession:
    def __init__(self, conversation, *scalar_results) -> None:
        self.conversation = conversation
        self.scalar_results = iter(scalar_results)
        self.added = None

    async def get(self, *_args, **_kwargs):
        return self.conversation

    async def scalar(self, _statement):
        return next(self.scalar_results)

    def add(self, value) -> None:
        self.added = value

    async def flush(self) -> None:
        return None


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


@pytest.mark.asyncio
async def test_legacy_confirmed_order_preserves_no_send_disposition() -> None:
    source = SimpleNamespace(
        message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    outbound_message_id = uuid.uuid4()
    conversation = SimpleNamespace(
        ownership_version=3,
        ownership_updated_at=datetime.now(timezone.utc),
        owner_type="ai",
        ai_execution_state="eligible",
    )
    resolved_draft = SimpleNamespace(
        resolution_outbound_message_id=outbound_message_id,
        order_id=uuid.uuid4(),
        resolved_at=datetime.now(timezone.utc),
    )

    session = _LegacySession(conversation, None, resolved_draft, source.message_id)
    lifecycle = await turn_recovery._legacy_lifecycle(session, source)

    assert session.added is lifecycle
    assert lifecycle.state == "skipped"
    assert lifecycle.disposition_code == "order_committed_no_send"
    assert lifecycle.outcome_type == "draft_reply"
    assert lifecycle.outbound_message_id == outbound_message_id
    assert lifecycle.ownership_version == 3


@pytest.mark.asyncio
async def test_legacy_audit_cannot_cross_an_ownership_generation() -> None:
    source = SimpleNamespace(
        message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    committed_at = datetime.now(timezone.utc)
    outbound_message_id = uuid.uuid4()
    audit = SimpleNamespace(
        outcome="response_generated",
        outbound_message_id=outbound_message_id,
        created_at=committed_at,
    )
    conversation = SimpleNamespace(
        ownership_version=4,
        ownership_updated_at=committed_at + timedelta(seconds=1),
        owner_type="ai",
        ai_execution_state="eligible",
    )

    session = _LegacySession(conversation, audit, None, source.message_id)
    lifecycle = await turn_recovery._legacy_lifecycle(session, source)

    assert lifecycle.state == "skipped"
    assert lifecycle.disposition_code == "ownership_changed_before_send"
    assert lifecycle.outcome_type == "response"
    assert lifecycle.outbound_message_id == outbound_message_id


@pytest.mark.asyncio
async def test_legacy_draft_reply_cannot_cross_an_ownership_generation() -> None:
    source = SimpleNamespace(
        message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
    )
    resolved_at = datetime.now(timezone.utc)
    outbound_message_id = uuid.uuid4()
    resolved_draft = SimpleNamespace(
        resolution_outbound_message_id=outbound_message_id,
        order_id=None,
        resolved_at=resolved_at,
    )
    conversation = SimpleNamespace(
        ownership_version=4,
        ownership_updated_at=resolved_at + timedelta(seconds=1),
        owner_type="ai",
        ai_execution_state="eligible",
    )

    session = _LegacySession(conversation, None, resolved_draft, source.message_id)
    lifecycle = await turn_recovery._legacy_lifecycle(session, source)

    assert lifecycle.state == "skipped"
    assert lifecycle.disposition_code == "ownership_changed_before_send"
    assert lifecycle.outcome_type == "draft_reply"
    assert lifecycle.outbound_message_id == outbound_message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["baileys", "official"])
async def test_legacy_send_requires_durable_channel_reconciliation(monkeypatch, mode):
    import app.config as config

    monkeypatch.setattr(config, "get_settings", lambda: SimpleNamespace(whatsapp_mode=mode))
    committed_at = datetime.now(timezone.utc)
    source = SimpleNamespace(message_id=uuid.uuid4(), conversation_id=uuid.uuid4())
    conversation = SimpleNamespace(
        ownership_version=1,
        ownership_updated_at=committed_at - timedelta(seconds=1),
        owner_type="ai",
        ai_execution_state="eligible",
    )
    audit = SimpleNamespace(
        outcome="response_generated",
        outbound_message_id=uuid.uuid4(),
        created_at=committed_at,
    )
    session = _LegacySession(conversation, audit, None, source.message_id)
    lifecycle = await turn_recovery._legacy_lifecycle(session, source)
    assert lifecycle.state == (
        "outcome_committed" if mode == "baileys" else "send_uncertain"
    )
