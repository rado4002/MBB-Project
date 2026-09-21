from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ai import ops
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService
from app.models.ai_turn_audit import AITurnAudit
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.inbound_turn_lifecycle import InboundTurnLifecycle
from app.models.message import Message
from app.modules.m1_gateway.turn_recovery import (
    TurnClaim,
    add_pending_turn,
    claim_turn,
    mark_outcome_committed,
    source_is_latest,
)
from app.modules.m4_conversation.ownership import ai_may_reply
from app.tasks import m1


DATABASE_URL = os.environ.get("AI6_2_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI6_2_TEST_DATABASE_URL is required for recovery PostgreSQL evidence",
)


class _Task:
    def __init__(self, task_id: str):
        self.request = SimpleNamespace(id=task_id, retries=0)

    def retry(self, **_kwargs):
        raise AssertionError("unexpected Celery retry")


class _Messaging:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls: list[tuple[str, str, str | None]] = []

    async def send_message(self, phone, value, *, idempotency_key=None):
        self.calls.append((phone, value, idempotency_key))
        if self.error is not None:
            raise self.error
        return "provider-confirmed-id"


class _HandoffAdapter:
    def __init__(self):
        self.calls = 0

    async def generate_turn(self, _request):
        self.calls += 1
        if self.calls > 1:
            pytest.fail("provider reran after committed terminal handoff")
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="handoff-once",
                    capability_name="request_human_handoff",
                    arguments={"reason_category": "customer_requested_human"},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
        )


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    value = create_async_engine(DATABASE_URL, pool_size=5)
    async with value.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE")
        )
    try:
        yield value
    finally:
        async with value.begin() as connection:
            await connection.execute(
                text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE")
            )
        await value.dispose()


@pytest_asyncio.fixture(loop_scope="function")
async def factory(engine, monkeypatch):
    import app.database as database

    value = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", value)
    return value


async def _seed(factory, *, offset_seconds: int = 0):
    now = datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)
    customer = Customer(
        phone_number="+243810006200",
        name="Recovery Fixture",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="active",
        language_detected="french",
        context={},
        message_count=1,
        owner_type="ai",
        human_owner_account_id=None,
        ai_execution_state="eligible",
        ownership_version=1,
        ownership_updated_at=now,
        start_time=now,
        last_message_time=now,
        created_at=now,
        updated_at=now,
    )
    inbound = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=now,
        direction="inbound",
        content="Je veux une aide test.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"recovery-{uuid.uuid4()}",
        created_at=now,
    )
    async with factory() as session:
        session.add_all((customer, conversation, inbound))
        await session.flush()
        add_pending_turn(
            session,
            source_message_id=inbound.message_id,
            conversation_id=conversation.conversation_id,
            ownership_version=conversation.ownership_version,
        )
        await session.commit()
    return customer, conversation, inbound


async def _persist_output(factory, conversation, inbound, *, outcome="response"):
    first = await claim_turn(inbound.message_id, attempt_id="persist-output")
    assert first.action == "process"
    return await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content="Réponse durable test.",
        language="french",
        processing_time_ms=5,
        expected_ownership_version=1,
        source_message_id=inbound.message_id,
        outcome_type=outcome,
    )


def _payload(customer, inbound):
    return {
        "message_id": str(inbound.message_id),
        "customer_phone": customer.phone_number,
        "content": inbound.content,
        "content_type": inbound.content_type,
        "timestamp": inbound.timestamp.isoformat(),
        "whatsapp_message_id": inbound.whatsapp_message_id,
    }


@pytest.mark.asyncio
async def test_inbound_commit_and_provider_result_windows_are_recoverable(factory):
    _customer, _conversation, inbound = await _seed(factory)

    after_inbound_commit = await claim_turn(
        inbound.message_id,
        attempt_id="redelivered-celery-task",
    )
    assert after_inbound_commit == TurnClaim(
        action="process",
        state="processing",
        ownership_version=1,
    )

    # A provider result has no authority of its own. Until output/domain commit,
    # the same redelivered Celery attempt must regenerate the turn.
    after_uncommitted_provider_result = await claim_turn(
        inbound.message_id,
        attempt_id="redelivered-celery-task",
    )
    assert after_uncommitted_provider_result.action == "process"
    async with factory() as session:
        lifecycle = await session.get(InboundTurnLifecycle, inbound.message_id)
        assert lifecycle is not None
        assert lifecycle.state == "processing"
        assert lifecycle.outbound_message_id is None


@pytest.mark.asyncio
async def test_concurrent_claims_choose_one_worker(factory):
    _customer, _conversation, inbound = await _seed(factory)
    claims = await asyncio.gather(
        claim_turn(inbound.message_id, attempt_id="worker-a"),
        claim_turn(inbound.message_id, attempt_id="worker-b"),
    )
    assert sorted(claim.action for claim in claims) == ["busy", "process"]


@pytest.mark.asyncio
async def test_outbound_commit_reuses_uuid_and_completed_redelivery_is_noop(
    factory, monkeypatch
):
    import app.adapters as adapters

    customer, conversation, inbound = await _seed(factory)
    outbound_id = await _persist_output(factory, conversation, inbound)
    assert outbound_id is not None
    async with factory() as session:
        session.add(
            Message(
                message_id=uuid.uuid4(),
                conversation_id=conversation.conversation_id,
                timestamp=inbound.timestamp + timedelta(seconds=1),
                direction="inbound",
                content="Newer inbound after output commit.",
                content_type="text",
                language="french",
                whatsapp_message_id=f"newer-after-output-{uuid.uuid4()}",
                created_at=inbound.created_at + timedelta(seconds=1),
            )
        )
        await session.commit()

    messaging = _Messaging()
    monkeypatch.setattr(adapters, "get_messaging_adapter", lambda: messaging)
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=True, m1_maps_fanout_enabled=False),
    )

    recovered = await m1._process(
        task=_Task("recover-outbound"),
        **_payload(customer, inbound),
    )
    assert recovered["status"] == "recovered"
    assert recovered["outbound_message_id"] == str(outbound_id)
    assert messaging.calls == [
        (customer.phone_number, "Réponse durable test.", str(outbound_id))
    ]

    replay = await m1._process(
        task=_Task("duplicate-after-complete"),
        **_payload(customer, inbound),
    )
    assert replay["status"] == "send_completed"
    assert messaging.calls == [
        (customer.phone_number, "Réponse durable test.", str(outbound_id))
    ]


@pytest.mark.asyncio
async def test_send_attempt_is_durable_uncertain_and_never_blindly_retried(
    factory, monkeypatch
):
    import app.adapters as adapters

    customer, conversation, inbound = await _seed(factory)
    outbound_id = await _persist_output(factory, conversation, inbound)
    messaging = _Messaging(error=TimeoutError("synthetic acknowledgement loss"))
    monkeypatch.setattr(adapters, "get_messaging_adapter", lambda: messaging)
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=True, m1_maps_fanout_enabled=False),
    )

    uncertain = await m1._process(
        task=_Task("uncertain-send"),
        **_payload(customer, inbound),
    )
    assert uncertain["send_status"] == "unknown_or_failed"
    assert messaging.calls == [
        (customer.phone_number, "Réponse durable test.", str(outbound_id))
    ]

    replay = await m1._process(
        task=_Task("uncertain-redelivery"),
        **_payload(customer, inbound),
    )
    assert replay["status"] == "send_uncertain"
    assert len(messaging.calls) == 1


@pytest.mark.asyncio
async def test_newer_inbound_and_ownership_change_fail_closed_before_inference(
    factory,
):
    customer, conversation, inbound = await _seed(factory)
    newer = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=inbound.timestamp + timedelta(seconds=1),
        direction="inbound",
        content="Message plus récent.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"newer-{uuid.uuid4()}",
        created_at=inbound.created_at + timedelta(seconds=1),
    )
    async with factory() as session:
        session.add(newer)
        await session.commit()

    superseded = await m1._process(
        task=_Task("stale-source"),
        **_payload(customer, inbound),
    )
    assert superseded["status"] == "superseded"

    _customer2, conversation2, inbound2 = await _seed_second(factory)
    async with factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation2.conversation_id)
            .values(
                owner_type="ai",
                ai_execution_state="eligible",
                ownership_version=2,
            )
        )
        await session.commit()
    changed = await m1._process(
        task=_Task("ownership-changed"),
        **_payload(_customer2, inbound2),
    )
    assert changed["status"] == "human_controlled"


async def _seed_second(factory):
    now = datetime.now(timezone.utc) + timedelta(minutes=1)
    customer = Customer(
        phone_number="+243810006201",
        name="Second Recovery Fixture",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="active",
        language_detected="french",
        context={},
        message_count=1,
        owner_type="ai",
        ai_execution_state="eligible",
        ownership_version=1,
        ownership_updated_at=now,
        start_time=now,
        last_message_time=now,
        created_at=now,
        updated_at=now,
    )
    inbound = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=now,
        direction="inbound",
        content="Deuxième test.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"second-{uuid.uuid4()}",
        created_at=now,
    )
    async with factory() as session:
        session.add_all((customer, conversation, inbound))
        await session.flush()
        add_pending_turn(
            session,
            source_message_id=inbound.message_id,
            conversation_id=conversation.conversation_id,
            ownership_version=1,
        )
        await session.commit()
    return customer, conversation, inbound


@pytest.mark.asyncio
async def test_terminal_handoff_commit_is_replayed_without_second_action(
    factory, monkeypatch
):
    customer, conversation, inbound = await _seed(factory)
    claimed = await claim_turn(inbound.message_id, attempt_id="terminal-turn")
    assert claimed.action == "process"

    adapter = _HandoffAdapter()

    async def authority(context):
        async with factory() as session:
            return await ai_may_reply(
                session,
                context.conversation_id,
                expected_ownership_version=context.expected_ownership_version,
            )

    service = AITurnService(
        adapter,
        authority_checker=authority,
        durable_session_factory=factory,
        turn_lifecycle_recorder=mark_outcome_committed,
    )
    finalized = await service.generate_finalized(
        AITurn(
            user_content=inbound.content,
            language="french",
            expected_ownership_version=1,
            conversation_id=conversation.conversation_id,
            source_message_id=inbound.message_id,
            allowed_capabilities=("request_human_handoff",),
        )
    )
    assert finalized.audit_persisted

    async with factory() as session:
        assert await session.scalar(select(func.count(EscalationTicket.ticket_id))) == 1
        assert await session.scalar(select(func.count(AITurnAudit.turn_id))) == 1
        lifecycle = await session.get(InboundTurnLifecycle, inbound.message_id)
        assert lifecycle is not None
        assert lifecycle.state == "outcome_committed"
        assert lifecycle.outcome_type == "handoff"
        outbound_id = lifecycle.outbound_message_id

    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=False, m1_maps_fanout_enabled=False),
    )
    recovered = await m1._process(
        task=_Task("terminal-redelivery"),
        **_payload(customer, inbound),
    )
    assert recovered["outbound_message_id"] == str(outbound_id)
    assert recovered["send_status"] == "skipped"
    assert adapter.calls == 1

    await m1._process(
        task=_Task("terminal-second-redelivery"),
        **_payload(customer, inbound),
    )
    async with factory() as session:
        assert await session.scalar(select(func.count(EscalationTicket.ticket_id))) == 1
        assert await session.scalar(select(func.count(AITurnAudit.turn_id))) == 1
    assert adapter.calls == 1


@pytest.mark.asyncio
async def test_telemetry_failure_does_not_change_recovery(factory, monkeypatch):
    import app.adapters as adapters

    customer, conversation, inbound = await _seed(factory)
    await _persist_output(factory, conversation, inbound)
    messaging = _Messaging()
    monkeypatch.setattr(adapters, "get_messaging_adapter", lambda: messaging)
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(whatsapp_send_enabled=True, m1_maps_fanout_enabled=False),
    )
    monkeypatch.setattr(
        ops,
        "emit",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("telemetry")),
    )

    result = await m1._process(
        task=_Task("telemetry-failure"),
        **_payload(customer, inbound),
    )
    assert result["send_status"] == "sent"
    async with factory() as session:
        lifecycle = await session.get(InboundTurnLifecycle, inbound.message_id)
        assert lifecycle is not None
        assert lifecycle.state == "send_completed"
