"""Offline observations and behavior equivalence; no provider or service egress."""

import asyncio
import io
import json
import logging
import threading
import uuid
from types import SimpleNamespace

import anthropic
import httpx
import pytest
import structlog

from app.ai import ops
from app.ai.capabilities import CapabilityTransactionRetry
from app.ai.provider_contract import ProviderErrorCategory, ProviderTurnError
from app.ai.turn import AITurnExecutionError, AITurnPersistenceError, AITurnService
from app.adapters.ai.claude_adapter import ClaudeAdapter
from app.adapters.ai.deepseek_adapter import DeepSeekAdapter
from app.tasks import m1

# Reuse the affected suites' scripted business seams, never live adapters/DBs.
import test_ai_turn_service as turns
import test_deepseek_adapter as deepseek
import test_m1_outbound_idempotency as gateway

SECRET = "FORBIDDEN_CUSTOMER_PROMPT_REASONING_ARGUMENT_RESULT_CREDENTIAL_URL"


@pytest.fixture
def records(monkeypatch):
    records = []
    monkeypatch.setattr(
        ops, "get_settings", lambda: SimpleNamespace(ai_ops_enabled=True)
    )
    monkeypatch.setattr(ops, "_enqueue", records.append)
    yield records
    assert SECRET not in json.dumps(records)
    assert ops._context.get() is None
    assert ops._counts.get() is None


def finished(records, event):
    return [
        r for r in records if r["observation"] == event and r["outcome"] != "started"
    ]


def broken(*args, **kwargs):
    raise RuntimeError(SECRET)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "provider", "cancel", "setup"])
async def test_complete_interrupted_and_setup_turns(records, failure):
    error = None
    if failure == "provider":
        error = ProviderTurnError(ProviderErrorCategory.timeout)
    if failure == "cancel":
        error = asyncio.CancelledError(SECRET)
    service = AITurnService(turns._RecordingAdapter(result=SECRET, error=error))
    turn = turns._turn(
        user_content=SECRET,
        history=({"content": SECRET},),
        source_message_id=uuid.uuid4(),
        allowed_capabilities=("search_products",) if failure == "setup" else (),
    )
    if failure:
        expected = {
            "provider": AITurnExecutionError,
            "cancel": asyncio.CancelledError,
            "setup": ProviderTurnError,
        }[failure]
        with pytest.raises(expected) as caught:
            await service.generate_finalized(turn)
        if failure == "cancel":
            assert caught.value is error
    else:
        assert (await service.generate_finalized(turn)).text == SECRET
    (observation,) = finished(records, "turn")
    assert (
        observation["outcome"]
        == {
            None: "response_generated",
            "provider": "failed",
            "cancel": "cancelled",
            "setup": "failed",
        }[failure]
    )
    assert observation["provider_calls"] == (0 if failure == "setup" else 1)
    assert observation["provider_attempts"] == 0  # scripted adapter has no transport
    assert observation["logical_capabilities"] == 0
    assert observation["turn_id"] == str(turn.turn_id)
    assert observation["source_message_id"] == str(turn.source_message_id)
    assert observation["duration_ms"] >= 0
    assert observation["observed_at"].endswith("+00:00")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "timeout", "cancel", "malformed"])
@pytest.mark.parametrize("usage", [None, {"prompt_tokens": 0, "completion_tokens": 3}])
@pytest.mark.parametrize("faulty_emitter", [False, True])
async def test_deepseek_actual_attempts_and_nullable_usage(
    records, monkeypatch, failure, usage, faulty_emitter
):
    if faulty_emitter:
        monkeypatch.setattr(ops, "emit", broken)
    response = deepseek._response(content=SECRET, reasoning_content=SECRET)
    if usage is None:
        response.pop("usage")
    else:
        response["usage"] = usage
    if failure == "malformed":
        response["choices"] = []
    error = {
        None: None,
        "timeout": httpx.ReadTimeout(SECRET),
        "cancel": asyncio.CancelledError(SECRET),
        "malformed": None,
    }[failure]
    transport = deepseek._FakeTransport(response=response, error=error)
    adapter = DeepSeekAdapter(api_key=SECRET, model=SECRET, transport=transport)
    if failure:
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else ProviderTurnError
        ):
            await adapter.generate_turn(deepseek._request())
    else:
        await adapter.generate_turn(deepseek._request())
    assert len(transport.calls) == 1
    if faulty_emitter:
        assert not records
        return
    (observation,) = finished(records, "provider_attempt")
    assert observation["provider"] == "deepseek"
    assert observation["outcome"] == (
        "cancelled" if failure == "cancel" else "failed" if failure else "succeeded"
    )
    assert observation["input_tokens"] == (0 if usage and not failure else None)
    assert observation["output_tokens"] == (3 if usage and not failure else None)
    assert observation["total_tokens"] is None
    assert observation["reasoning_tokens"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [None, "connection", "cancel", "status", "circuit"])
@pytest.mark.parametrize("faulty_emitter", [False, True])
async def test_claude_sdk_attempts_preserve_retry_sequence(
    records, monkeypatch, failure, faulty_emitter
):
    if faulty_emitter:
        monkeypatch.setattr(ops, "emit", broken)
    adapter = object.__new__(ClaudeAdapter)
    adapter._failures = 0
    adapter._circuit_open = failure == "circuit"
    adapter._circuit_opened_at = __import__("time").monotonic()
    calls, delays = [], []
    request = httpx.Request("POST", "https://example.invalid")

    async def create(**kwargs):
        calls.append(kwargs)
        if failure == "connection":
            raise anthropic.APIConnectionError(message=SECRET, request=request)
        if failure == "cancel":
            raise asyncio.CancelledError(SECRET)
        if failure == "status":
            raise anthropic.APIStatusError(
                SECRET, response=httpx.Response(403, request=request), body=None
            )
        return SimpleNamespace(
            content=[SimpleNamespace(text=SECRET)],
            usage=SimpleNamespace(input_tokens=0, output_tokens=2),
        )

    async def sleep(delay):
        delays.append(delay)

    adapter._client = SimpleNamespace(messages=SimpleNamespace(create=create))
    monkeypatch.setattr(asyncio, "sleep", sleep)
    if failure:
        with pytest.raises(
            asyncio.CancelledError if failure == "cancel" else RuntimeError
        ):
            await adapter.generate_turn(deepseek._request())
    else:
        result = await adapter.generate_turn(deepseek._request())
        assert result.text == SECRET
        assert result.usage.input_tokens is None  # Legacy bridge is unchanged.
    assert len(calls) == (
        4 if failure == "connection" else 0 if failure == "circuit" else 1
    )
    assert delays == ([2.0, 5.0, 10.0] if failure == "connection" else [])
    if faulty_emitter:
        assert not records
        return
    observations = finished(records, "provider_attempt")
    assert len(observations) == len(calls)
    if not failure:
        assert observations[0]["input_tokens"] == 0
        assert observations[0]["output_tokens"] == 2
        assert observations[0]["total_tokens"] is None
    if failure == "cancel":
        assert observations[0]["outcome"] == "cancelled"


@pytest.mark.asyncio
@pytest.mark.parametrize("audit_fails", [False, True])
@pytest.mark.parametrize("faulty_emitter", [False, True])
async def test_logical_capability_spans_all_transaction_retries(
    records, monkeypatch, audit_fails, faulty_emitter
):
    if faulty_emitter:
        monkeypatch.setattr(ops, "emit", broken)
    session = turns._TransactionSession()
    executions = []

    async def handler(runtime_session, context, arguments):
        executions.append(1)
        if len(executions) == 1:
            raise CapabilityTransactionRetry()
        return {"value": SECRET, "trusted_conversation_id": context.conversation_id}

    async def audit(runtime_session, record):
        if audit_fails:
            raise RuntimeError(SECRET)
        runtime_session.events.append("audit")

    service = AITurnService(
        turns._SequenceAdapter(
            turns._tool_result(
                turns._tool_call(name="terminal_action", arguments={"value": SECRET})
            )
        ),
        capability_registry=turns._terminal_registry(handler),
        authority_checker=turns._authority_allowed,
        durable_session_factory=lambda: turns._TransactionContext(session),
        audit_appender=audit,
    )
    if audit_fails:
        with pytest.raises(AITurnPersistenceError):
            await service.generate_finalized(
                turns._turn(allowed_capabilities=("terminal_action",))
            )
    else:
        assert (
            await service.generate_finalized(
                turns._turn(allowed_capabilities=("terminal_action",))
            )
        ).audit_persisted
    assert len(executions) == 2
    assert session.events == (
        ["rollback", "rollback"] if audit_fails else ["rollback", "audit", "commit"]
    )
    if faulty_emitter:
        assert not records
        return
    assert len(finished(records, "capability")) == 1
    assert finished(records, "capability")[0]["capability"] == "unknown"
    persistence = finished(records, "persistence")
    assert [r["outcome"] for r in persistence] == [
        "retry",
        "failed" if audit_fails else "committed",
    ]
    assert [r["transaction_outcome"] for r in persistence] == [
        "rolled_back",
        "rolled_back" if audit_fails else "committed",
    ]
    assert finished(records, "turn")[0]["logical_capabilities"] == 1
    assert finished(records, "turn")[0]["persistence_attempts"] == 2
    assert session.events == (
        ["rollback", "rollback"] if audit_fails else ["rollback", "audit", "commit"]
    )


@pytest.mark.asyncio
async def test_grounding_and_stale_are_distinct(records, monkeypatch):
    import app.ai.turn as module
    from app.ai.commercial_grounding import (
        CommercialGroundingDiagnostic,
        CommercialGroundingError,
    )

    def reject(*args):
        raise CommercialGroundingError(
            CommercialGroundingDiagnostic(
                category="authoritative_price_mismatch",
                assertion_role="product_price",
                claim_currency=SECRET,
                claim_amount=None,
                assertion_start=0,
                claim_start=0,
                claim_end=1,
            )
        )

    with monkeypatch.context() as patch:
        patch.setattr(module, "validate_commercial_grounding", reject)
        with pytest.raises(AITurnExecutionError):
            await AITurnService(turns._RecordingAdapter()).generate_finalized(
                turns._turn()
            )
    assert finished(records, "grounding")[0]["outcome"] == "rejected"
    assert finished(records, "turn")[0]["reason"] == "commercial_grounding_failed"
    records.clear()

    async def stale(context):
        return False

    with pytest.raises(AITurnExecutionError):
        await AITurnService(
            turns._RecordingAdapter(), authority_checker=stale
        ).generate_finalized(turns._turn())
    assert finished(records, "turn")[0]["outcome"] == "stale"
    assert not finished(
        records, "grounding"
    )  # Absence means not evaluated, never passed.


@pytest.mark.parametrize(
    "failure", [None, "audit", "commit", "send", "fallback", "grounding"]
)
def test_m1_persistence_fallback_and_send_observations(records, monkeypatch, failure):
    events, messaging = gateway._patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        outbound_commit_error=RuntimeError(SECRET) if failure == "commit" else None,
        audit_error=RuntimeError(SECRET) if failure == "audit" else None,
        messaging_error=RuntimeError(SECRET) if failure == "send" else None,
        ai=gateway._FailingAI()
        if failure == "fallback"
        else gateway._GroundingFailingAI()
        if failure == "grounding"
        else None,
    )
    result = gateway._run(gateway._process(gateway._Task()))
    (persistence,) = [
        r for r in finished(records, "persistence") if r["boundary"] == "ordinary"
    ]
    assert persistence["outcome"] == (
        "rolled_back" if failure in {"audit", "commit"} else "committed"
    )
    assert persistence["transaction_outcome"] == persistence["outcome"]
    if failure in {"audit", "commit"}:
        assert result["status"] == "persistence_failed"
        assert not finished(records, "send")
        assert not messaging.calls
    else:
        assert finished(records, "send")[0]["outcome"] == (
            "uncertain" if failure == "send" else "confirmed"
        )
        assert events.index("commit", events.index("persist")) < events.index("adapter")
    if failure in {"fallback", "grounding"}:
        assert finished(records, "fallback")[0]["outcome"] == "selected"
        assert persistence["turn_outcome"] == "fallback_used"
        assert finished(records, "fallback")[0]["reason"] == (
            "provider_failure"
            if failure == "fallback"
            else "commercial_grounding_failed"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("handoff", [False, True])
@pytest.mark.parametrize(
    "result", ["confirmed", "skipped", "uncertain", "unverified", "cancelled"]
)
async def test_send_boundaries(records, monkeypatch, handoff, result):
    import app.adapters as adapters
    import app.database as database

    calls = []
    identifier = uuid.uuid4()

    async def send(*args, **kwargs):
        calls.append(kwargs)
        if result == "cancelled":
            raise asyncio.CancelledError(SECRET)
        return "" if result == "uncertain" else SECRET

    async def execute(statement):
        return SimpleNamespace(
            scalar_one_or_none=lambda: None if result == "unverified" else identifier
        )

    session = turns._TransactionSession()
    session.execute = execute
    monkeypatch.setattr(
        database, "async_session_factory", lambda: turns._TransactionContext(session)
    )
    monkeypatch.setattr(
        adapters, "get_messaging_adapter", lambda: SimpleNamespace(send_message=send)
    )
    monkeypatch.setattr(
        m1, "settings", SimpleNamespace(whatsapp_send_enabled=result != "skipped")
    )
    if handoff:
        coroutine = m1._send_persisted_handoff_ack_safe(
            SECRET, SECRET, outbound_message_id=identifier, conversation_id=uuid.uuid4()
        )
    else:
        coroutine = m1._send_safe(SECRET, SECRET, idempotency_key=str(identifier))
    if result == "cancelled":
        with pytest.raises(asyncio.CancelledError):
            await coroutine
    else:
        await coroutine
    (observation,) = finished(records, "send")
    expected = (
        ("uncertain" if handoff else "confirmed") if result == "unverified" else result
    )
    assert observation["outcome"] == expected
    assert observation["send_result"] == (
        expected if expected in {"confirmed", "skipped"} else "uncertain"
    )
    assert observation["outbound_message_id"] == str(identifier)
    assert len(calls) == (
        0 if result == "skipped" or (handoff and result == "unverified") else 1
    )


@pytest.mark.parametrize("fault", ["emit", "queue", "clock", "projection", "config"])
@pytest.mark.parametrize("business_failure", [None, "commit", "send", "fallback"])
def test_telemetry_failures_leave_business_behavior_identical(
    monkeypatch, fault, business_failure
):
    snapshots = []
    for enabled in (False, True):
        with monkeypatch.context() as patch:
            patch.setattr(
                ops, "get_settings", lambda: SimpleNamespace(ai_ops_enabled=enabled)
            )
            fixed_id = uuid.UUID("10000000-0000-4000-8000-000000000001")
            patch.setattr(uuid, "uuid4", lambda: fixed_id)
            import app.ai.turn as turn_module

            original_turn = turn_module.AITurn

            def fixed_turn(**kwargs):
                turn = original_turn(**kwargs)
                object.__setattr__(turn, "turn_id", fixed_id)
                return turn

            patch.setattr(turn_module, "AITurn", fixed_turn)
            patch.setattr(m1, "time", SimpleNamespace(monotonic=lambda: 1.0))
            patch.setattr(ops, "_enqueue", lambda record: None)
            if enabled:
                if fault == "clock":
                    patch.setattr(ops, "time", SimpleNamespace(monotonic=broken))
                else:
                    patch.setattr(
                        ops,
                        {
                            "emit": "emit",
                            "queue": "_enqueue",
                            "projection": "_project",
                            "config": "get_settings",
                        }[fault],
                        broken,
                    )
            events, messaging = gateway._patch_normal_flow(
                patch,
                outbound_id=uuid.uuid4(),
                outbound_commit_error=RuntimeError(SECRET)
                if business_failure == "commit"
                else None,
                messaging_error=RuntimeError(SECRET)
                if business_failure == "send"
                else None,
                ai=gateway._FailingAI() if business_failure == "fallback" else None,
            )
            task = gateway._Task()
            result = gateway._run(gateway._process(task))
            snapshots.append(
                (
                    result,
                    events,
                    messaging.calls,
                    messaging.persisted_contents,
                    [r.model_dump() for r in messaging.audits],
                    task.retry_calls,
                )
            )
    assert snapshots[0] == snapshots[1]
    assert ops._context.get() is None


def test_allowlist_and_rendered_privacy(records, monkeypatch):
    ops.emit(
        "turn",
        "succeeded",
        prompt=SECRET,
        customer_text=SECRET,
        reasoning=SECRET,
        tool_arguments={"value": SECRET},
        tool_results=SECRET,
        commercial_payload=SECRET,
        exception=RuntimeError(SECRET),
        credentials=SECRET,
        url=SECRET,
        model=SECRET,
        provider=SECRET,
        capability=SECRET,
        reason=SECRET,
        turn_id=SECRET,
        input_tokens=True,
        output_tokens=-1,
    )
    ops.emit(SECRET, "succeeded")
    ops.emit("turn", SECRET)
    assert len(records) == 1
    assert records[0]["provider"] == records[0]["capability"] == "unknown"
    assert "turn_id" not in records[0]
    assert "input_tokens" not in records[0]
    output = io.StringIO()
    monkeypatch.setattr(ops, "sys", SimpleNamespace(stdout=output))
    structlog.contextvars.bind_contextvars(customer_text=SECRET)
    try:
        sink = ops._Sink()
        sink.handle(records[0])
        assert json.loads(output.getvalue())["event"] == "ai_ops.v1"
        assert SECRET not in output.getvalue()
        monkeypatch.setattr(sink, "logger", SimpleNamespace(info=broken))
        sink.handle(records[0])  # Sink failure cannot escape or recursively log.
    finally:
        structlog.contextvars.clear_contextvars()


def test_bounded_nonblocking_queue_and_process_reset(monkeypatch):
    entered, release = threading.Event(), threading.Event()

    class BlockedSink(logging.Handler):
        def emit(self, record):
            entered.set()
            release.wait(5)

    monkeypatch.setattr(
        ops, "get_settings", lambda: SimpleNamespace(ai_ops_enabled=True)
    )
    monkeypatch.setattr(ops, "_Sink", BlockedSink)
    ops._after_fork()
    try:
        ops.emit("turn", "started")
        assert entered.wait(2)
        for _ in range(ops._BUFFER_SIZE + 20):
            ops.emit("turn", "succeeded")
        assert ops._buffer.qsize() == ops._BUFFER_SIZE
        # Caller has returned with sink still blocked; overflow was dropped.
        assert not release.is_set()
    finally:
        release.set()
        ops._buffer.join()
        ops._listener.stop()
        ops._after_fork()
    assert ops._buffer is None and ops._listener is None and ops._pid is None
    assert ops._init_lock.acquire(blocking=False)
    ops._init_lock.release()


def test_blocked_telemetry_does_not_hold_general_printlogger_lock(monkeypatch):
    entered, release, business_written = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )

    class Output:
        def write(self, value):
            if "ai_ops.v1" in value:
                entered.set()
                release.wait(5)
            elif "business" in value:
                business_written.set()

        def flush(self):
            pass

    output = Output()
    monkeypatch.setattr(ops, "sys", SimpleNamespace(stdout=output))
    monkeypatch.setattr(
        ops, "get_settings", lambda: SimpleNamespace(ai_ops_enabled=True)
    )
    ops._after_fork()
    try:
        ops.emit("turn", "started")
        assert entered.wait(2)
        business = threading.Thread(
            target=lambda: structlog.PrintLogger(output).info("business"), daemon=True
        )
        business.start()
        assert business_written.wait(1)
        assert not release.is_set()
        business.join(1)
    finally:
        release.set()
        ops._buffer.join()
        ops._listener.stop()
        ops._after_fork()


@pytest.mark.asyncio
async def test_concurrent_turns_keep_attempt_counts_and_ids_separate(records):
    gate = asyncio.Event()
    entered = []

    class Transport:
        async def create_chat_completion(self, payload):
            entered.append(1)
            if len(entered) == 2:
                gate.set()
            await gate.wait()
            return deepseek._response(content=SECRET, reasoning_content=SECRET)

    requests = [
        turns._turn(conversation_id=uuid.uuid4(), source_message_id=uuid.uuid4())
        for _ in range(2)
    ]
    services = [
        AITurnService(DeepSeekAdapter(api_key=SECRET, transport=Transport()))
        for _ in requests
    ]
    results = await asyncio.gather(
        *(service.generate_finalized(turn) for service, turn in zip(services, requests))
    )
    assert [result.text for result in results] == [SECRET, SECRET]
    for turn in requests:
        own = [
            record for record in records if record.get("turn_id") == str(turn.turn_id)
        ]
        (final,) = finished(own, "turn")
        assert final["provider_calls"] == final["provider_attempts"] == 1
        assert final["logical_capabilities"] == final["tool_rounds"] == 0
        assert all("conversation_id" not in record for record in own)
        (attempt,) = finished(own, "provider_attempt")
        assert attempt["provider_call_index"] == attempt["attempt_index"] == 1
        assert attempt["input_tokens"] == 17
        assert attempt["returned_tool_calls"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_disabled_provider_never_claims_actual_attempt(
    records, monkeypatch, enabled
):
    from app.adapters.ai.disabled_adapter import DisabledAIAdapter

    monkeypatch.setattr(
        ops, "get_settings", lambda: SimpleNamespace(ai_ops_enabled=enabled)
    )
    with pytest.raises(AITurnExecutionError):
        await AITurnService(DisabledAIAdapter()).generate_finalized(turns._turn())
    if enabled:
        (final,) = finished(records, "turn")
        assert final["provider_calls"] == 1
        assert final["provider_attempts"] == 0
    else:
        assert records == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    ["ai_authority_changed", "newer_customer_evidence", "commercial_state_changed"],
)
async def test_ordinary_persistence_observes_stale_rollback(
    records, monkeypatch, reason
):
    import app.database as database
    import app.ai.commercial_state as state

    turn = turns._turn(source_message_id=uuid.uuid4())
    session = turns._TransactionSession()

    async def authority(*args, **kwargs):
        return reason != "ai_authority_changed"

    async def latest(statement):
        return (
            uuid.uuid4()
            if reason == "newer_customer_evidence"
            else turn.source_message_id
        )

    async def read(*args):
        return SimpleNamespace(revision=1)

    session.scalar = latest
    monkeypatch.setattr(m1, "_ai_may_reply", authority)
    monkeypatch.setattr(
        database, "async_session_factory", lambda: turns._TransactionContext(session)
    )
    monkeypatch.setattr(state, "read_commercial_state", read)
    result = await m1._persist_outbound(
        conversation_id=turn.conversation_id,
        content=SECRET,
        language="french",
        processing_time_ms=0,
        expected_ownership_version=7,
        source_message_id=turn.source_message_id,
        audit_record=gateway._audit_record(turn),
        expected_commercial_state_revision=0,
    )
    assert result is None
    assert session.events == ["rollback"]
    (observation,) = finished(records, "persistence")
    assert observation["outcome"] == "stale"
    assert observation["reason"] == reason
    assert observation["transaction_outcome"] == "rolled_back"


@pytest.mark.asyncio
async def test_handoff_verification_exception_is_not_converted_to_a_result(
    records, monkeypatch
):
    import app.database as database

    error = RuntimeError(SECRET)
    session = turns._TransactionSession()

    async def execute(statement):
        raise error

    session.execute = execute
    monkeypatch.setattr(
        database, "async_session_factory", lambda: turns._TransactionContext(session)
    )
    monkeypatch.setattr(m1, "settings", SimpleNamespace(whatsapp_send_enabled=True))
    with pytest.raises(RuntimeError) as caught:
        await m1._send_persisted_handoff_ack_safe(
            SECRET,
            SECRET,
            outbound_message_id=uuid.uuid4(),
            conversation_id=uuid.uuid4(),
        )
    assert caught.value is error
    assert session.events == []
    (observation,) = finished(records, "send")
    assert observation["outcome"] == "failed"
    assert observation["send_result"] == "uncertain"


@pytest.mark.parametrize("failure", [None, "commit", "fallback", "send"])
def test_worker_correlation_includes_persistence_and_send(
    records, monkeypatch, failure
):
    gateway._patch_normal_flow(
        monkeypatch,
        outbound_id=uuid.uuid4(),
        outbound_commit_error=RuntimeError(SECRET) if failure == "commit" else None,
        messaging_error=RuntimeError(SECRET) if failure == "send" else None,
        ai=gateway._FailingAI() if failure == "fallback" else None,
    )
    task = gateway._Task()
    task.request.id = str(uuid.uuid4())
    task.request.delivery_info = {"redelivered": False, "private": SECRET}
    result = gateway._run(gateway._process(task))
    (final,) = finished(records, "worker")
    assert final["worker_status"] == result["status"]
    assert final["task_id"] == task.request.id
    assert final["task_retries"] == 0 and final["redelivered"] is False
    assert final["duration_ms"] >= 0
    assert records[0]["observation"] == records[-1]["observation"] == "worker"
    assert all(r["task_id"] == task.request.id for r in records)
    assert all(
        r["source_message_id"] == "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        for r in records
    )
    if failure == "commit":
        assert not finished(records, "send")
    else:
        assert final["worker_send_result"] == (
            "uncertain" if failure == "send" else "confirmed"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("error_kind", ["retry", "cancel"])
async def test_worker_preserves_retry_and_cancellation(records, error_kind):
    from celery.exceptions import Retry

    error = Retry(SECRET) if error_kind == "retry" else asyncio.CancelledError(SECRET)

    @ops.worker
    async def invocation(**kwargs):
        raise error

    task = SimpleNamespace(
        request=SimpleNamespace(
            id=str(uuid.uuid4()),
            retries=2,
            delivery_info={"redelivered": True},
        )
    )
    with pytest.raises(type(error)) as caught:
        await invocation(task=task, message_id=str(uuid.uuid4()))
    assert caught.value is error
    (final,) = finished(records, "worker")
    assert final["outcome"] == ("retry" if error_kind == "retry" else "cancelled")
    assert final["task_retries"] == 2 and final["redelivered"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario", ["broker", "blackout", "lost", "duplicate", "limited"]
)
@pytest.mark.parametrize("fault", [None, "disabled", "queue"])
async def test_acceptance_publication_observations(
    records, monkeypatch, scenario, fault
):
    from datetime import datetime, timezone
    from fastapi import HTTPException
    from app.api.v1 import messages
    from app.schemas.messages import InboundMessageRequest
    from app.tasks.celery_app import celery_app

    events, published = [], []
    task_id, source_id = str(uuid.uuid4()), uuid.uuid4()
    payload = InboundMessageRequest(
        message_id=source_id,
        customer_phone="+243810000041",
        content=SECRET,
        content_type="text",
        timestamp=datetime(2000, 1, 1, tzinfo=timezone.utc),
        whatsapp_message_id=SECRET,
    )

    async def duplicate(*args):
        return scenario == "duplicate"

    async def limited(*args):
        return scenario == "limited"

    def publish(*args, **kwargs):
        events.append("publish")
        published.append(kwargs)
        if scenario in {"blackout", "lost"}:
            raise RuntimeError(SECRET)
        return SimpleNamespace(id=task_id)

    async def blackout(value):
        events.append("blackout")
        assert value == published[0]["kwargs"]
        return scenario == "blackout"

    async def mark(*args):
        events.append("mark")
        return True

    monkeypatch.setattr(messages, "has_accepted_inbound", duplicate)
    monkeypatch.setattr(messages, "rate_limit_check", limited)
    monkeypatch.setattr(messages, "blackout_enqueue", blackout)
    monkeypatch.setattr(messages, "mark_inbound_accepted", mark)
    monkeypatch.setattr(celery_app, "send_task", publish)
    if fault == "disabled":
        monkeypatch.setattr(
            ops, "get_settings", lambda: SimpleNamespace(ai_ops_enabled=False)
        )
    elif fault == "queue":
        monkeypatch.setattr(ops, "_enqueue", broken)
    if scenario in {"lost", "limited"}:
        with pytest.raises(HTTPException) as caught:
            await messages._handle_inbound(payload=payload)
        assert caught.value.status_code == (503 if scenario == "lost" else 429)
    else:
        result = await messages._handle_inbound(payload=payload)
        assert result.status == ("duplicate" if scenario == "duplicate" else "queued")
    assert (
        events
        == {
            "broker": ["publish", "mark"],
            "blackout": ["publish", "blackout", "mark"],
            "lost": ["publish", "blackout"],
            "duplicate": [],
            "limited": [],
        }[scenario]
    )
    if published:
        assert set(published[0]["kwargs"]) == {
            "message_id",
            "customer_phone",
            "content",
            "content_type",
            "timestamp",
            "whatsapp_message_id",
        }
        assert set(published[0]) == {"kwargs", "queue"}
    if fault:
        assert records == []
        return
    (acceptance,) = finished(records, "acceptance")
    assert (
        acceptance["outcome"]
        == {
            "broker": "accepted",
            "blackout": "accepted",
            "lost": "unconfirmed",
            "duplicate": "duplicate",
            "limited": "rate_limited",
        }[scenario]
    )
    assert acceptance["source_message_id"] == str(source_id)
    if published:
        (publication,) = finished(records, "publication")
        assert publication["outcome"] == (
            "confirmed" if scenario == "broker" else "unconfirmed"
        )
        assert publication["duration_ms"] >= 0
        assert publication["observed_at"] <= acceptance["observed_at"]
        assert publication["observed_at"].startswith("2000") is False
        if scenario == "broker":
            assert publication["task_id"] == task_id
        else:
            assert "task_id" not in publication


def test_draft_commit_and_skipped_send_are_observed(records, monkeypatch):
    gateway.test_exact_order_draft_reply_is_handled_before_provider_inference(
        monkeypatch
    )
    (persistence,) = finished(records, "persistence")
    assert persistence["boundary"] == "draft_reply"
    assert persistence["outcome"] == persistence["transaction_outcome"] == "committed"
    assert persistence["draft_state"] == "confirmed"
    (worker,) = finished(records, "worker")
    assert worker["worker_status"] == "order_draft_confirmed"
    assert worker["worker_send_result"] == "skipped"
    assert not finished(records, "send")  # No fabricated duration for an uncalled send.
    assert not finished(records, "turn")


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("fault", [False, True])
async def test_blackout_publication_correlation_and_ack(
    records, monkeypatch, failure, fault
):
    from unittest.mock import AsyncMock
    import test_blackout_claim_ack as blackout

    payload = blackout._payload()
    raw = json.dumps(payload)
    task_id = str(uuid.uuid4())
    events = []

    def publish(*args, **kwargs):
        assert kwargs == {"kwargs": payload, "queue": "default"}
        events.append("publish")
        if failure:
            raise RuntimeError(SECRET)
        return SimpleNamespace(id=task_id)

    async def ack(value):
        assert value == raw
        events.append("ack")
        return True

    if fault:
        monkeypatch.setattr(ops, "emit", broken)
    stack, helpers = blackout.CanonicalDrainTests()._patch_helpers(
        [raw, None],
        blackout_acknowledge=AsyncMock(side_effect=ack),
    )
    monkeypatch.setattr(m1.celery_app, "send_task", publish)
    with stack:
        result = await m1._drain(SimpleNamespace())
    assert events == (["publish"] if failure else ["publish", "ack"])
    assert result["published"] == (0 if failure else 1)
    if failure:
        helpers["blackout_acknowledge"].assert_not_awaited()
    if fault:
        assert records == []
    else:
        (final,) = finished(records, "publication")
        assert final["route"] == "blackout_replay"
        assert final["source_message_id"] == payload["message_id"]
        assert final["outcome"] == ("unconfirmed" if failure else "confirmed")
        if not failure:
            assert final["task_id"] == task_id


@pytest.mark.parametrize("failure", ["commit", "stale"])
def test_draft_failure_never_reports_committed_success(records, monkeypatch, failure):
    import app.modules.m7_conversion.order_drafts as drafts

    events, messaging = gateway._patch_normal_flow(
        monkeypatch, outbound_id=uuid.uuid4()
    )

    async def handle_reply(session, **kwargs):
        if failure == "stale":
            raise drafts.StaleOrderDraftAuthority
        session.commit_error = RuntimeError(SECRET)
        return gateway.OrderDraftReplyResult(
            state="confirmed",
            draft_id=uuid.uuid4(),
            draft_version=1,
            customer_text=SECRET,
            outbound_message_id=uuid.uuid4(),
            order_id=uuid.uuid4(),
        )

    monkeypatch.setattr(drafts, "handle_order_draft_reply", handle_reply)
    result = gateway._run(gateway._process(gateway._Task()))
    (final,) = finished(records, "persistence")
    assert final["outcome"] == ("stale" if failure == "stale" else "rolled_back")
    assert final["transaction_outcome"] == "rolled_back"
    assert "draft_state" not in final
    assert not messaging.calls and not finished(records, "send")
    assert result["status"] == (
        "stale_order_draft_authority" if failure == "stale" else "persistence_failed"
    )


def test_monotonic_duration_and_unknown_invalid_measurements(records, monkeypatch):
    times = iter([10.0, 10.125])
    monkeypatch.setattr(ops, "time", SimpleNamespace(monotonic=lambda: next(times)))
    with ops.observe("publication", source_message_id=uuid.uuid4()):
        pass
    (final,) = finished(records, "publication")
    assert final["duration_ms"] == 125.0
    for invalid in (-1, float("nan"), float("inf"), True, SECRET):
        ops.emit(
            "worker",
            "succeeded",
            duration_ms=invalid,
            task_retries=invalid,
            worker_status=SECRET,
            worker_send_result=SECRET,
            route=SECRET,
            draft_state=SECRET,
            redelivered=SECRET,
            conversation_id=uuid.uuid4(),
        )
        assert records[-1]["duration_ms"] is None
        assert "task_retries" not in records[-1]
        assert "redelivered" not in records[-1]
        assert "conversation_id" not in records[-1]
        assert records[-1]["worker_status"] == "unknown"


@pytest.mark.asyncio
async def test_worker_context_survives_ai_turn_and_transport(records):
    task = SimpleNamespace(
        request=SimpleNamespace(
            id=str(uuid.uuid4()),
            retries=0,
            delivery_info={"redelivered": False},
        )
    )
    turn = turns._turn(source_message_id=uuid.uuid4())

    class Transport:
        async def create_chat_completion(self, payload):
            return deepseek._response(content=SECRET, reasoning_content=SECRET)

    service = AITurnService(DeepSeekAdapter(api_key=SECRET, transport=Transport()))

    @ops.worker
    async def invocation(**kwargs):
        await service.generate_finalized(turn)
        return {"status": "processed"}

    await invocation(task=task, message_id=str(turn.source_message_id))
    assert {"worker", "turn", "provider_call", "provider_attempt"} <= {
        record["observation"] for record in records
    }
    assert all(record["task_id"] == task.request.id for record in records)
    assert all(
        record["source_message_id"] == str(turn.source_message_id) for record in records
    )
    (final,) = finished(records, "turn")
    assert final["provider_calls"] == final["provider_attempts"] == 1
