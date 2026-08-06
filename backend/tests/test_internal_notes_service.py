from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.config import Settings
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.models.message import Message
from app.modules.m4_conversation import internal_notes
from app.modules.m4_conversation.internal_notes import (
    InternalNoteIdempotencyConflict,
    InternalNoteUnavailable,
    create_internal_note,
)
from app.operator_identity import audit as operator_audit


class FakeSession:
    def __init__(self, *, conversation_exists: bool = True) -> None:
        self.conversation_exists = conversation_exists
        self.conversation = SimpleNamespace(
            last_message_time=datetime(2026, 8, 1, tzinfo=timezone.utc),
            message_count=7,
            status="active",
            owner_type="ai",
            human_owner_account_id=None,
            ai_execution_state="eligible",
        )
        self.notes: dict[uuid.UUID, InternalNote] = {}
        self.pending: list[Any] = []
        self.commits = 0
        self.rollbacks = 0

    async def get(self, model, identity):
        if model is InternalNote:
            return self.notes.get(identity)
        if model is Conversation:
            return self.conversation if self.conversation_exists else None
        raise AssertionError(f"unexpected model lookup: {model}")

    def add(self, value: Any) -> None:
        self.pending.append(value)

    async def flush(self) -> None:
        for value in self.pending:
            if isinstance(value, InternalNote) and value.created_at is None:
                value.created_at = datetime.now(timezone.utc)

    async def commit(self) -> None:
        for value in self.pending:
            if isinstance(value, InternalNote):
                self.notes[value.note_id] = value
        self.pending.clear()
        self.commits += 1

    async def rollback(self) -> None:
        self.pending.clear()
        self.rollbacks += 1


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        browser_idempotency_hmac_secret="i" * 32,
    )


async def _create(
    session: FakeSession,
    *,
    note_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_id: uuid.UUID,
    content: str,
):
    return await create_internal_note(
        session,  # type: ignore[arg-type]
        note_id=note_id,
        conversation_id=conversation_id,
        content=content,
        actor_account_id=actor_id,
        actor_display_name="Operator One",
        actor_role="operator",
        idempotency_secret="i" * 32,
        request_id="internal-note-test",
        settings=_settings(),
        source_network_fingerprint="network-fingerprint",
        user_agent_fingerprint="agent-fingerprint",
    )


@pytest.mark.asyncio
async def test_creation_is_exactly_replayable_atomic_and_message_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()
    note_id = uuid.uuid4()
    conversation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    content = "  Note interne — 你好\n<script>alert(1)</script>  "
    audit_calls: list[dict[str, Any]] = []
    conversation_state = vars(session.conversation).copy()

    async def _audit(_session, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(internal_notes, "append_operator_audit_event", _audit)

    first = await _create(
        session,
        note_id=note_id,
        conversation_id=conversation_id,
        actor_id=actor_id,
        content=content,
    )
    replay = await _create(
        session,
        note_id=note_id,
        conversation_id=conversation_id,
        actor_id=actor_id,
        content=content,
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert replay.note is first.note
    assert first.note.content == content
    assert list(session.notes) == [note_id]
    assert session.commits == 1
    assert len(audit_calls) == 1
    audit = audit_calls[0]
    assert audit["action"] == "internal_note_created"
    assert audit["target_id"] == str(note_id)
    assert audit["request_id"] == "internal-note-test"
    assert audit["actor_account_id"] == actor_id
    assert audit["metadata"] == {
        "conversation_id": str(conversation_id),
        "character_count": len(content),
        "source": "operator_browser",
    }
    assert content not in repr(audit)
    assert "content" not in repr(audit["metadata"]).lower()
    assert all(not isinstance(item, Message) for item in session.pending)
    assert vars(session.conversation) == conversation_state


@pytest.mark.asyncio
async def test_key_reuse_conflicts_for_changed_payload_actor_or_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()

    async def _audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(internal_notes, "append_operator_audit_event", _audit)
    key = uuid.uuid4()
    conversation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    await _create(
        session,
        note_id=key,
        conversation_id=conversation_id,
        actor_id=actor_id,
        content="first",
    )

    for changed in (
        {"content": "second"},
        {"actor_id": uuid.uuid4()},
        {"conversation_id": uuid.uuid4()},
    ):
        with pytest.raises(InternalNoteIdempotencyConflict):
            await _create(
                session,
                note_id=key,
                conversation_id=changed.get("conversation_id", conversation_id),
                actor_id=changed.get("actor_id", actor_id),
                content=changed.get("content", "first"),
            )

    assert len(session.notes) == 1


@pytest.mark.asyncio
async def test_distinct_keys_create_distinct_notes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = FakeSession()

    async def _audit(*_args, **_kwargs):
        return None

    monkeypatch.setattr(internal_notes, "append_operator_audit_event", _audit)
    conversation_id = uuid.uuid4()
    actor_id = uuid.uuid4()
    for text in ("one", "two"):
        await _create(
            session,
            note_id=uuid.uuid4(),
            conversation_id=conversation_id,
            actor_id=actor_id,
            content=text,
        )
    assert {note.content for note in session.notes.values()} == {"one", "two"}


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_note(monkeypatch: pytest.MonkeyPatch) -> None:
    session = FakeSession()

    async def _audit(*_args, **_kwargs):
        raise SQLAlchemyError("audit unavailable")

    monkeypatch.setattr(internal_notes, "append_operator_audit_event", _audit)
    with pytest.raises(InternalNoteUnavailable):
        await _create(
            session,
            note_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
            actor_id=uuid.uuid4(),
            content="must be atomic",
        )
    assert session.notes == {}
    assert session.pending == []
    assert session.commits == 0
    assert session.rollbacks == 1


@pytest.mark.parametrize(
    "field_name",
    ["note_text", "note_content", "internal_note_content"],
)
def test_audit_metadata_denylist_rejects_note_content_fields(
    field_name: str,
) -> None:
    with pytest.raises(ValueError, match="sensitive audit metadata is forbidden"):
        operator_audit._validate_metadata({field_name: "must never be audited"})
