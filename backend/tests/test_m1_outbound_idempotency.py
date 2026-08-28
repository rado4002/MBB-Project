import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.ai.audit import AITurnAuditRecord, AITurnOutcome
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.turn import AITurnExecutionError, FinalizedAITurnResult
from app.modules.m1_gateway.service import ProcessedInbound
from app.modules.m1_gateway.session_cache import SessionState
from app.tasks import m1

_DEFAULT_CACHED_SESSION = object()


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _Session:
    def __init__(self, events, *, commit_error=None, query_rows=None):
        self.events = events
        self.commit_error = commit_error
        self.query_rows = query_rows
        self.statements = []

    async def commit(self):
        self.events.append("commit")
        if self.commit_error:
            raise self.commit_error

    async def rollback(self):
        self.events.append("rollback")

    async def execute(self, statement):
        self.statements.append(statement)
        if self.query_rows is None:
            raise AssertionError("unexpected database query")
        return _ScalarResult(self.query_rows)


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *_args):
        return False


class _Task:
    def __init__(self):
        self.request = SimpleNamespace(retries=0)
        self.retry_calls = 0

    def retry(self, **_kwargs):
        self.retry_calls += 1
        raise AssertionError("Celery retry called")


def _audit_record(turn, *, outcome=AITurnOutcome.response_generated, safe_code=None):
    return AITurnAuditRecord(
        turn_id=turn.turn_id,
        conversation_id=turn.conversation_id,
        source_message_id=turn.source_message_id,
        policy_version=AI_SYSTEM_POLICY_VERSION,
        provider="scripted",
        model="offline-fixture",
        exposed_capabilities=tuple(sorted(turn.allowed_capabilities)),
        outcome=outcome,
        safe_code=safe_code,
    )


class _AI:
    async def generate_finalized(self, turn):
        return FinalizedAITurnResult(
            text="outbound response",
            audit_record=_audit_record(turn),
        )


class _FailingAI:
    async def generate_finalized(self, turn):
        raise AITurnExecutionError(
            _audit_record(
                turn,
                outcome=AITurnOutcome.failed,
                safe_code="provider_failure",
            ),
            RuntimeError("AI unavailable"),
        )


class _UnfinalizedFailingAI:
    async def generate_finalized(self, _turn):
        raise RuntimeError("unexpected service contract failure")


class _RecordingAI:
    def __init__(self):
        self.turns = []

    async def generate_finalized(self, turn):
        self.turns.append(turn)
        return FinalizedAITurnResult(
            text="outbound response",
            audit_record=_audit_record(turn),
        )


class _Messaging:
    def __init__(self, events, *, error=None):
        self.events = events
        self.error = error
        self.calls = []
        self.audits = []
        self.saved_session_states = []
        self.inbound_session = None

    async def send_message(self, phone, text, *, idempotency_key=None):
        self.events.append("adapter")
        self.calls.append((phone, text, idempotency_key))
        if self.error:
            raise self.error
        return "provider-456"


def _run(coro):
    return asyncio.run(coro)


def _patch_normal_flow(
    monkeypatch,
    *,
    outbound_id,
    outbound_commit_error=None,
    messaging_error=None,
    audit_error=None,
    ai=None,
    cached_session=_DEFAULT_CACHED_SESSION,
    database_history=(),
):
    import app.adapters as adapters
    import app.ai.turn as ai_turn
    import app.database as database
    import app.modules.m1_gateway.service as service
    import app.modules.m1_gateway.session_cache as session_cache
    import app.modules.m4_conversation.engine as conversation_engine

    events = []
    inbound_session = _Session(events, query_rows=database_history)
    outbound_session = _Session(events, commit_error=outbound_commit_error)
    send_session = _Session(events)
    sessions = iter((inbound_session, outbound_session, send_session))
    conversation_id = uuid.uuid4()
    inbound = ProcessedInbound(
        customer_phone="+243812345678",
        conversation_id=conversation_id,
        message_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        language="french",
    )
    messaging = _Messaging(events, error=messaging_error)
    messaging.inbound_session = inbound_session

    async def process_inbound(**_kwargs):
        events.append("inbound")
        return inbound

    async def persist_outbound(**_kwargs):
        events.append("persist")
        return outbound_id

    async def append_audit(_session, record):
        events.append("audit")
        if audit_error is not None:
            raise audit_error
        messaging.audits.append(record)
        return SimpleNamespace(turn_id=record.turn_id)

    async def get_session(_conversation_id):
        if cached_session is not _DEFAULT_CACHED_SESSION:
            return cached_session
        return SessionState(
            customer_id="+243812345678",
            language="french",
            stage="active",
            ownership_version=4,
        )

    async def save_session(_conversation_id, state):
        events.append("cache")
        messaging.saved_session_states.append(state)
        return True

    monkeypatch.setattr(
        database,
        "async_session_factory",
        lambda: _SessionContext(next(sessions)),
    )
    monkeypatch.setattr(service, "process_inbound", process_inbound)
    monkeypatch.setattr(service, "persist_outbound", persist_outbound)
    monkeypatch.setattr(m1, "append_ai_turn_audit", append_audit)
    monkeypatch.setattr(session_cache, "get_session", get_session)
    monkeypatch.setattr(session_cache, "save_session", save_session)
    monkeypatch.setattr(ai_turn, "get_ai_turn_service", lambda: ai or _AI())
    monkeypatch.setattr(adapters, "get_messaging_adapter", lambda: messaging)
    monkeypatch.setattr(
        conversation_engine,
        "detect_qualification_signals",
        lambda _content: False,
    )
    monkeypatch.setattr(m1, "_dispatch_maps_fanout", lambda **_kwargs: None)
    async def _allow_ai(*_args, **_kwargs):
        return True

    monkeypatch.setattr(m1, "_ai_may_reply", _allow_ai)

    async def _ownership_version(*_args, **_kwargs):
        return 4

    monkeypatch.setattr(m1, "_ai_reply_ownership_version", _ownership_version)
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(
            whatsapp_send_enabled=True,
            m1_maps_fanout_enabled=False,
        ),
    )
    return events, messaging


def _process(task):
    return m1._process(
        task=task,
        message_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
        customer_phone="+243812345678",
        content="inbound content",
        content_type="text",
        timestamp="2026-01-01T00:00:00+00:00",
        whatsapp_message_id="wa-inbound-id",
    )


def test_committed_outbound_uuid_reaches_send_safe_and_adapter(monkeypatch):
    outbound_id = uuid.uuid4()
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=outbound_id,
    )
    task = _Task()

    result = _run(_process(task))

    assert messaging.calls == [
        ("+243812345678", "outbound response", str(outbound_id))
    ]
    persisted_at = events.index("persist")
    assert events[persisted_at : persisted_at + 3] == ["persist", "audit", "commit"]
    assert persisted_at < events.index("adapter")
    assert len(messaging.audits) == 1
    assert messaging.audits[0].outbound_message_id == outbound_id
    assert result["outbound_message_id"] == str(outbound_id)
    assert result["send_status"] == "sent"
    assert result["provider_message_id"] == "provider-456"
    assert task.retry_calls == 0


def test_m1_binds_trusted_context_and_explicit_capability_exposure(monkeypatch):
    ai = _RecordingAI()
    _events, _messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        ai=ai,
    )

    result = _run(_process(_Task()))

    assert len(ai.turns) == 1
    turn = ai.turns[0]
    assert str(turn.conversation_id) == result["conversation_id"]
    assert turn.expected_ownership_version == 4
    assert turn.allowed_capabilities == m1._M1_AI_CAPABILITIES
    assert turn.source_message_id == uuid.UUID(
        "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    )


def test_cold_history_excludes_the_current_inbound_from_ai_context(monkeypatch):
    ai = _RecordingAI()
    prior_messages = (
        SimpleNamespace(
            direction="outbound",
            content="Ancienne réponse",
            language="french",
        ),
        SimpleNamespace(
            direction="inbound",
            content="Ancienne question",
            language="french",
        ),
    )
    _events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        ai=ai,
        cached_session=None,
        database_history=prior_messages,
    )

    _run(_process(_Task()))

    assert ai.turns[0].user_content == "inbound content"
    assert [item["content"] for item in ai.turns[0].history] == [
        "Ancienne question",
        "Ancienne réponse",
    ]
    assert "inbound content" not in {
        item["content"] for item in ai.turns[0].history
    }
    assert "messages.message_id !=" in str(
        messaging.inbound_session.statements[0]
    )


def test_cache_from_an_older_ownership_period_is_rebuilt_from_db(monkeypatch):
    ai = _RecordingAI()
    stale_cache = SessionState(
        ownership_version=3,
        history=[
            {
                "direction": "outbound",
                "content": "stale cached AI history",
                "language": "french",
            }
        ],
    )
    human_period_messages = (
        SimpleNamespace(
            direction="inbound",
            content="Réponse après le contrôle humain",
            language="french",
        ),
    )
    _events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        ai=ai,
        cached_session=stale_cache,
        database_history=human_period_messages,
    )

    _run(_process(_Task()))

    assert [item["content"] for item in ai.turns[0].history] == [
        "Réponse après le contrôle humain"
    ]
    assert messaging.saved_session_states[0].ownership_version == 4


def test_human_ownership_stops_generation_persistence_and_send(monkeypatch):
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
    )

    async def _no_ai_generation(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        m1,
        "_ai_reply_ownership_version",
        _no_ai_generation,
    )

    async def _not_waiting(*_args, **_kwargs):
        return False

    monkeypatch.setattr(m1, "_ai_is_waiting_for_human", _not_waiting)
    result = _run(_process(_Task()))

    assert result["status"] == "human_controlled"
    assert result["send_status"] == "skipped"
    assert events == ["inbound", "commit", "rollback"]
    assert messaging.calls == []


def test_waiting_for_human_suppresses_ai_without_claiming_human_ownership(
    monkeypatch,
):
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
    )

    async def _no_ai_generation(*_args, **_kwargs):
        return None

    async def _waiting(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        m1,
        "_ai_reply_ownership_version",
        _no_ai_generation,
    )
    monkeypatch.setattr(m1, "_ai_is_waiting_for_human", _waiting)

    result = _run(_process(_Task()))

    assert result["status"] == "waiting_for_human"
    assert result["send_status"] == "skipped"
    assert events == ["inbound", "commit", "rollback"]
    assert messaging.calls == []


def test_ai_failure_preserves_localized_fallback_and_outbound_path(monkeypatch):
    from app.i18n.messages import t

    outbound_id = uuid.uuid4()
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=outbound_id,
        ai=_FailingAI(),
    )

    result = _run(_process(_Task()))

    fallback = t("error_fallback", "french")
    assert messaging.calls == [
        ("+243812345678", fallback, str(outbound_id))
    ]
    persisted_at = events.index("persist")
    assert events[persisted_at : persisted_at + 3] == ["persist", "audit", "commit"]
    assert messaging.audits[0].outcome == AITurnOutcome.fallback_used.value
    assert result["status"] == "processed"
    assert result["outbound_message_id"] == str(outbound_id)


def test_unfinalized_ai_failure_cannot_persist_unaudited_fallback(monkeypatch):
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        ai=_UnfinalizedFailingAI(),
    )

    result = _run(_process(_Task()))

    assert result["status"] == "persistence_failed"
    assert result["send_status"] == "unknown_or_failed"
    assert events == ["inbound", "commit", "rollback"]
    assert messaging.audits == []
    assert messaging.calls == []


def test_outbound_commit_failure_is_fail_closed_without_fallback_uuid(monkeypatch):
    persisted_id = uuid.uuid4()
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=persisted_id,
        outbound_commit_error=RuntimeError("commit failed"),
    )
    monkeypatch.setattr(
        m1.uuid,
        "uuid4",
        lambda: pytest.fail("replacement UUID generated"),
    )
    task = _Task()

    result = _run(_process(task))

    assert result["status"] == "persistence_failed"
    assert result["send_status"] == "unknown_or_failed"
    assert messaging.calls == []
    assert "adapter" not in events
    assert "rollback" in events
    assert task.retry_calls == 0


def test_audit_failure_rolls_back_outbound_and_prevents_send(monkeypatch):
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        audit_error=RuntimeError("fictional audit failure"),
    )

    result = _run(_process(_Task()))

    assert result["status"] == "persistence_failed"
    assert messaging.calls == []
    assert "adapter" not in events
    assert events.count("commit") == 1
    persisted_at = events.index("persist")
    assert events[persisted_at : persisted_at + 3] == [
        "persist",
        "audit",
        "rollback",
    ]


def test_send_exception_is_unknown_without_celery_retry(monkeypatch):
    outbound_id = uuid.uuid4()
    _events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=outbound_id,
        messaging_error=TimeoutError(
            "+243812345678 private-message secret-provider-object"
        ),
    )
    records = []
    monkeypatch.setattr(
        m1,
        "log",
        SimpleNamespace(
            info=lambda *args, **kwargs: records.append(("info", args, kwargs)),
            warning=lambda *args, **kwargs: records.append(("warning", args, kwargs)),
            error=lambda *args, **kwargs: records.append(("error", args, kwargs)),
        ),
    )
    task = _Task()

    result = _run(_process(task))

    assert len(messaging.calls) == 1
    assert result["send_status"] == "unknown_or_failed"
    assert "provider_message_id" not in result
    assert task.retry_calls == 0
    serialized = repr(records)
    for sensitive in (
        "+243812345678",
        "private-message",
        "secret-provider-object",
        str(outbound_id),
    ):
        assert sensitive not in serialized


def test_disabled_sending_is_honestly_skipped_without_adapter_lookup(monkeypatch):
    import app.adapters as adapters

    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=False),
    )
    monkeypatch.setattr(
        adapters,
        "get_messaging_adapter",
        lambda: pytest.fail("adapter lookup occurred"),
    )

    result = _run(
        m1._send_safe(
            "+243812345678",
            "private-message",
            idempotency_key=str(uuid.uuid4()),
        )
    )

    assert result == {"status": "skipped"}


def test_last_moment_human_takeover_blocks_adapter_send(monkeypatch):
    import app.adapters as adapters
    import app.database as database

    events = []
    session = _Session(events)

    async def _pause_ai(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=True),
    )
    monkeypatch.setattr(m1, "_ai_may_reply", _pause_ai)
    monkeypatch.setattr(
        database,
        "async_session_factory",
        lambda: _SessionContext(session),
    )
    monkeypatch.setattr(
        adapters,
        "get_messaging_adapter",
        lambda: pytest.fail("adapter lookup occurred"),
    )

    result = _run(
        m1._send_safe(
            "+243812345678",
            "private-message",
            idempotency_key=str(uuid.uuid4()),
            conversation_id=uuid.uuid4(),
            expected_ownership_version=4,
        )
    )

    assert result == {"status": "skipped"}
    assert events == ["rollback"]


def test_complete_human_cycle_does_not_reauthorize_stale_ai_send(monkeypatch):
    import app.adapters as adapters
    import app.database as database

    events = []
    session = _Session(events)
    observed_versions = []

    async def _generation_gate(
        *_args, expected_ownership_version=None, **_kwargs
    ):
        observed_versions.append(expected_ownership_version)
        return False

    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=True),
    )
    monkeypatch.setattr(m1, "_ai_may_reply", _generation_gate)
    monkeypatch.setattr(
        database,
        "async_session_factory",
        lambda: _SessionContext(session),
    )
    monkeypatch.setattr(
        adapters,
        "get_messaging_adapter",
        lambda: pytest.fail("stale turn reached the messaging adapter"),
    )

    result = _run(
        m1._send_safe(
            "+243812345678",
            "stale generation N output",
            idempotency_key=str(uuid.uuid4()),
            conversation_id=uuid.uuid4(),
            expected_ownership_version=4,
        )
    )

    assert result == {"status": "skipped"}
    assert observed_versions == [4]


def test_empty_provider_id_is_unknown_or_failed(monkeypatch):
    import app.adapters as adapters

    messaging = _Messaging([], error=None)

    async def empty_send(*_args, **_kwargs):
        return ""

    messaging.send_message = empty_send
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=True),
    )
    monkeypatch.setattr(adapters, "get_messaging_adapter", lambda: messaging)

    result = _run(
        m1._send_safe(
            "+243812345678",
            "private-message",
            idempotency_key=str(uuid.uuid4()),
        )
    )

    assert result == {"status": "unknown_or_failed"}
