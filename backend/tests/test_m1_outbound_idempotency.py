import asyncio
import uuid
from types import SimpleNamespace

import pytest

from app.modules.m1_gateway.service import ProcessedInbound
from app.modules.m1_gateway.session_cache import SessionState
from app.tasks import m1


class _Session:
    def __init__(self, events, *, commit_error=None):
        self.events = events
        self.commit_error = commit_error

    async def commit(self):
        self.events.append("commit")
        if self.commit_error:
            raise self.commit_error

    async def rollback(self):
        self.events.append("rollback")

    async def execute(self, _statement):
        raise AssertionError("unexpected database query")


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


class _AI:
    async def generate(self, **_kwargs):
        return "outbound response"


class _Messaging:
    def __init__(self, events, *, error=None):
        self.events = events
        self.error = error
        self.calls = []

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
):
    import app.adapters as adapters
    import app.database as database
    import app.modules.m1_gateway.service as service
    import app.modules.m1_gateway.session_cache as session_cache
    import app.modules.m4_conversation.engine as conversation_engine

    events = []
    inbound_session = _Session(events)
    outbound_session = _Session(events, commit_error=outbound_commit_error)
    send_session = _Session(events)
    sessions = iter((inbound_session, outbound_session, send_session))
    conversation_id = uuid.uuid4()
    inbound = ProcessedInbound(
        customer_phone="+243812345678",
        conversation_id=conversation_id,
        message_id=uuid.uuid4(),
        language="french",
    )
    messaging = _Messaging(events, error=messaging_error)

    async def process_inbound(**_kwargs):
        events.append("inbound")
        return inbound

    async def persist_outbound(**_kwargs):
        events.append("persist")
        return outbound_id

    async def get_session(_conversation_id):
        return SessionState(
            customer_id="+243812345678",
            language="french",
            stage="active",
        )

    async def save_session(_conversation_id, _state):
        events.append("cache")
        return True

    monkeypatch.setattr(
        database,
        "async_session_factory",
        lambda: _SessionContext(next(sessions)),
    )
    monkeypatch.setattr(service, "process_inbound", process_inbound)
    monkeypatch.setattr(service, "persist_outbound", persist_outbound)
    monkeypatch.setattr(session_cache, "get_session", get_session)
    monkeypatch.setattr(session_cache, "save_session", save_session)
    monkeypatch.setattr(adapters, "get_ai_adapter", lambda: _AI())
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
    assert events.index("persist") < events.index("commit", 2) < events.index("adapter")
    assert result["outbound_message_id"] == str(outbound_id)
    assert result["send_status"] == "sent"
    assert result["provider_message_id"] == "provider-456"
    assert task.retry_calls == 0


def test_human_ownership_stops_generation_persistence_and_send(monkeypatch):
    events, messaging = _patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
    )

    async def _pause_ai(*_args, **_kwargs):
        return False

    monkeypatch.setattr(m1, "_ai_may_reply", _pause_ai)
    result = _run(_process(_Task()))

    assert result["status"] == "human_controlled"
    assert result["send_status"] == "skipped"
    assert events == ["inbound", "commit", "rollback"]
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
        )
    )

    assert result == {"status": "skipped"}
    assert events == ["rollback"]


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
