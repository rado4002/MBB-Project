from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ai.audit import AITurnAuditRecord, AITurnOutcome
from app.ai.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    SearchProductsInput,
    SearchProductsOutput,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.provider_contract import (
    ProviderContinuationState,
    ProviderFinishReason,
    ProviderIdentity,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnPersistenceError, AITurnService
from app.models.ai_turn_audit import AITurnAudit
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.message import Message
from app.modules.m4_conversation.ai_handoff import apply_human_handoff
from app.modules.m4_conversation.ownership import ai_may_reply

DATABASE_URL = os.environ.get("AI1E_TEST_DATABASE_URL")
HIDDEN_REASONING_SENTINEL = "DO_NOT_PERSIST_REASONING_SENTINEL"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI1E_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL, pool_size=5)
    truncate = text(
        """
        TRUNCATE TABLE
            mbb.ai_turn_audits,
            mbb.escalation_tickets,
            mbb.messages,
            mbb.conversations,
            mbb.customers
        RESTART IDENTITY CASCADE
        """
    )
    async with database_engine.begin() as connection:
        await connection.execute(truncate)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(truncate)
        await database_engine.dispose()


async def _seed(
    engine: AsyncEngine,
) -> tuple[async_sessionmaker, Conversation, uuid.UUID]:
    now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    customer = Customer(
        phone_number="+243810000093",
        name="Fictional AI audit customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="qualifying",
        language_detected="french",
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
    source_message_id = uuid.uuid4()
    inbound = Message(
        message_id=source_message_id,
        conversation_id=conversation.conversation_id,
        timestamp=now,
        direction="inbound",
        content="Je veux parler a une personne.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"fictional-{source_message_id}",
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all((customer, conversation, inbound))
        await session.commit()
    return factory, conversation, source_message_id


def _response_audit(
    conversation_id: uuid.UUID,
    source_message_id: uuid.UUID,
) -> AITurnAuditRecord:
    return AITurnAuditRecord(
        turn_id=uuid.uuid4(),
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        policy_version=AI_SYSTEM_POLICY_VERSION,
        provider="scripted",
        model="offline-fixture",
        outcome=AITurnOutcome.response_generated,
    )


class _ScriptedHandoffAdapter:
    def __init__(self) -> None:
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        if len(self.calls) > 1:
            pytest.fail("provider continuation occurred after committed handoff")
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="handoff_call_1",
                    capability_name="request_human_handoff",
                    arguments={"reason_category": "customer_requested_human"},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=ProviderContinuationState(
                value={"hidden_reasoning": HIDDEN_REASONING_SENTINEL}
            ),
        )


class _ScriptedFinalAdapter:
    def __init__(self) -> None:
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        if len(self.calls) == 1:
            return ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="search_call_1",
                        capability_name="search_products",
                        arguments={
                            "query": "air fryer",
                            "search_mode": "SELLABLE_ONLY",
                        },
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
            )
        return ProviderTurnResult(
            text="Air fryer fictif disponible a 55 USD.",
            finish_reason=ProviderFinishReason.completed,
        )


def _scripted_product_registry() -> CapabilityRegistry:
    async def search_products(_context, _arguments):
        return {
            "items": [
                {
                    "product_id": uuid.UUID("20000000-0000-4000-8000-000000000002"),
                    "sellable_item_id": uuid.UUID(
                        "60000000-0000-4000-8000-000000000006"
                    ),
                    "name": "Air fryer fictif",
                    "model_label": "6L",
                    "category_code": "air_fryer",
                    "attributes": {"capacity_l": 6},
                    "current_usd_price": Decimal("55.00"),
                    "price_currency": "USD",
                    "cdf_quote_status": "available",
                    "derived_cdf_quote": {
                        "currency": "CDF",
                        "amount": Decimal("154000.00"),
                    },
                    "availability": "available",
                    "offer_status": "sellable_now",
                    "is_sellable_now": True,
                    "primary_media": None,
                }
            ]
        }

    return CapabilityRegistry(
        (
            CapabilityDefinition(
                name="search_products",
                description="Return the current fictional Product Offer.",
                input_model=SearchProductsInput,
                output_model=SearchProductsOutput,
                handler=search_products,
            ),
        )
    )


def _handoff_service(factory, *, audit_appender=None) -> AITurnService:
    async def authority_checker(context):
        async with factory() as session:
            return await ai_may_reply(
                session,
                context.conversation_id,
                expected_ownership_version=context.expected_ownership_version,
            )

    return AITurnService(
        _ScriptedHandoffAdapter(),
        authority_checker=authority_checker,
        durable_session_factory=factory,
        audit_appender=audit_appender,
        provider_identity=ProviderIdentity(
            provider="scripted",
            model="offline-fixture",
        ),
    )


@pytest.mark.asyncio
async def test_real_m1_scripted_response_persists_outbound_and_audit_atomically(
    engine,
    monkeypatch,
) -> None:
    import app.adapters as adapters
    import app.ai.turn as ai_turn
    import app.database as database
    import app.modules.m1_gateway.session_cache as session_cache
    import app.modules.m4_conversation.engine as conversation_engine
    from app.tasks import m1

    factory = async_sessionmaker(engine, expire_on_commit=False)
    scripted = _ScriptedFinalAdapter()

    async def authority_checker(context):
        async with factory() as session:
            return await ai_may_reply(
                session,
                context.conversation_id,
                expected_ownership_version=context.expected_ownership_version,
            )

    service = AITurnService(
        scripted,
        capability_registry=_scripted_product_registry(),
        authority_checker=authority_checker,
        durable_session_factory=factory,
        provider_identity=ProviderIdentity(
            provider="scripted",
            model="offline-fixture",
        ),
    )
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(ai_turn, "get_ai_turn_service", lambda: service)

    async def no_cached_session(_conversation_id):
        return None

    async def save_session(_conversation_id, _state):
        return True

    monkeypatch.setattr(session_cache, "get_session", no_cached_session)
    monkeypatch.setattr(session_cache, "save_session", save_session)
    monkeypatch.setattr(
        conversation_engine,
        "detect_qualification_signals",
        lambda _content: False,
    )
    monkeypatch.setattr(m1, "_dispatch_maps_fanout", lambda **_kwargs: None)
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(
            whatsapp_send_enabled=False,
            m1_maps_fanout_enabled=False,
        ),
    )
    monkeypatch.setattr(
        adapters,
        "get_messaging_adapter",
        lambda: pytest.fail("external messaging adapter was resolved"),
    )

    class _Task:
        request = SimpleNamespace(retries=0)

        def retry(self, **_kwargs):
            pytest.fail("unexpected Celery retry")

    source_message_id = uuid.uuid4()
    result = await m1._process(
        task=_Task(),
        message_id=str(source_message_id),
        customer_phone="+243810000094",
        content="Je cherche un air fryer a moins de 70 USD.",
        content_type="text",
        timestamp="2026-08-14T12:00:00+00:00",
        whatsapp_message_id=f"fictional-m1-{source_message_id}",
    )

    assert result["status"] == "processed"
    assert result["send_status"] == "skipped"
    assert len(scripted.calls) == 2
    conversation_id = uuid.UUID(result["conversation_id"])
    outbound_message_id = uuid.UUID(result["outbound_message_id"])
    async with factory() as session:
        messages = (
            await session.scalars(
                select(Message).where(Message.conversation_id == conversation_id)
            )
        ).all()
        audits = (
            await session.scalars(
                select(AITurnAudit).where(
                    AITurnAudit.conversation_id == conversation_id
                )
            )
        ).all()
        stored_conversation = await session.get(Conversation, conversation_id)

    assert sum(message.direction == "inbound" for message in messages) == 1
    assert sum(message.direction == "outbound" for message in messages) == 1
    assert len(audits) == 1
    assert audits[0].source_message_id == source_message_id
    assert audits[0].outbound_message_id == outbound_message_id
    assert audits[0].outcome == "response_generated"
    assert audits[0].provider == "scripted"
    assert audits[0].model == "offline-fixture"
    assert stored_conversation is not None
    assert stored_conversation.message_count == 2


@pytest.mark.asyncio
async def test_outbound_and_audit_commit_together(engine, monkeypatch) -> None:
    import app.database as database
    from app.tasks import m1

    factory, conversation, source_message_id = await _seed(engine)
    monkeypatch.setattr(database, "async_session_factory", factory)
    record = _response_audit(conversation.conversation_id, source_message_id)

    outbound_id = await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content="Reponse fictive auditee.",
        language="french",
        processing_time_ms=10,
        expected_ownership_version=1,
        source_message_id=source_message_id,
        audit_record=record,
    )

    assert outbound_id is not None
    async with factory() as session:
        messages = (
            await session.scalars(
                select(Message).where(
                    Message.conversation_id == conversation.conversation_id
                )
            )
        ).all()
        stored_audit = await session.get(AITurnAudit, record.turn_id)
        stored_conversation = await session.get(
            Conversation,
            conversation.conversation_id,
        )
        assert sum(message.direction == "inbound" for message in messages) == 1
        assert sum(message.direction == "outbound" for message in messages) == 1
        assert stored_audit is not None
        assert stored_audit.outbound_message_id == outbound_id
        assert stored_audit.source_message_id == source_message_id
        assert stored_conversation is not None
        assert stored_conversation.message_count == 2


@pytest.mark.asyncio
async def test_outbound_audit_failure_rolls_back_only_ai_action(
    engine,
    monkeypatch,
) -> None:
    import app.database as database
    from app.tasks import m1

    factory, conversation, source_message_id = await _seed(engine)
    monkeypatch.setattr(database, "async_session_factory", factory)

    async def fail_audit(_session, _record):
        raise RuntimeError("fictional audit persistence failure")

    monkeypatch.setattr(m1, "append_ai_turn_audit", fail_audit)
    persisted = await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content="Cette reponse ne doit pas persister.",
        language="french",
        processing_time_ms=10,
        expected_ownership_version=1,
        source_message_id=source_message_id,
        audit_record=_response_audit(
            conversation.conversation_id,
            source_message_id,
        ),
    )

    assert persisted is None
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation.conversation_id,
                    Message.direction == "inbound",
                )
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(
                    Message.conversation_id == conversation.conversation_id,
                    Message.direction == "outbound",
                )
            )
            == 0
        )
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 0
        stored_conversation = await session.get(
            Conversation,
            conversation.conversation_id,
        )
        assert stored_conversation is not None
        assert stored_conversation.message_count == 1


@pytest.mark.asyncio
async def test_caller_owned_handoff_can_roll_back_without_partial_state(engine) -> None:
    factory, conversation, _source_message_id = await _seed(engine)

    async with factory() as session:
        result = await apply_human_handoff(
            session,
            conversation_id=conversation.conversation_id,
            expected_ownership_version=1,
        )
        assert result.ownership_version == 2
        await session.rollback()

    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        assert stored is not None
        assert stored.ai_execution_state == "eligible"
        assert stored.ownership_version == 1
        assert (
            await session.scalar(select(func.count()).select_from(EscalationTicket))
            == 0
        )


@pytest.mark.asyncio
async def test_handoff_and_audit_commit_atomically_and_reasoning_is_excluded(
    engine,
) -> None:
    factory, conversation, source_message_id = await _seed(engine)
    service = _handoff_service(factory)

    finalized = await service.generate_finalized(
        AITurn(
            user_content="Je veux parler a une personne.",
            language="french",
            expected_ownership_version=1,
            conversation_id=conversation.conversation_id,
            source_message_id=source_message_id,
            allowed_capabilities=("request_human_handoff",),
        )
    )

    assert (
        finalized.text == "D'accord. Je transmets cette conversation à un conseiller."
    )
    assert finalized.outbound_message_id is not None
    assert finalized.audit_persisted is True
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        audit = await session.get(AITurnAudit, finalized.audit_record.turn_id)
        tickets = (
            await session.scalars(
                select(EscalationTicket).where(
                    EscalationTicket.conversation_id == conversation.conversation_id
                )
            )
        ).all()
        assert stored is not None
        assert stored.owner_type == "ai"
        assert stored.human_owner_account_id is None
        assert stored.ai_execution_state == "paused"
        assert stored.ownership_version == 2
        assert len(tickets) == 1
        assert tickets[0].status == "open"
        assert audit is not None
        assert audit.outcome == "handoff_requested"
        assert audit.outbound_message_id == finalized.outbound_message_id
        assert audit.capability_activity == [
            {
                "capability_name": "request_human_handoff",
                "decision": "executed",
                "outcome": "success",
                "safe_code": None,
                "handoff_reason": "explicit_human_request",
            }
        ]
        serialized_audit = json.dumps(
            {
                column.name: getattr(audit, column.name)
                for column in audit.__table__.columns
            },
            default=str,
            sort_keys=True,
        )
        assert HIDDEN_REASONING_SENTINEL not in serialized_audit
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.direction == "outbound")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_handoff_audit_failure_rolls_back_authority_ticket_and_audit(
    engine,
) -> None:
    factory, conversation, source_message_id = await _seed(engine)

    async def fail_audit(_session, _record):
        raise RuntimeError("fictional audit persistence failure")

    service = _handoff_service(factory, audit_appender=fail_audit)
    with pytest.raises(AITurnPersistenceError, match="ai_turn_persistence_failed"):
        await service.generate_finalized(
            AITurn(
                user_content="Je veux parler a une personne.",
                language="french",
                expected_ownership_version=1,
                conversation_id=conversation.conversation_id,
                source_message_id=source_message_id,
                allowed_capabilities=("request_human_handoff",),
            )
        )

    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        assert stored is not None
        assert stored.owner_type == "ai"
        assert stored.human_owner_account_id is None
        assert stored.ai_execution_state == "eligible"
        assert stored.ownership_version == 1
        assert (
            await session.scalar(select(func.count()).select_from(EscalationTicket))
            == 0
        )
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 0
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.direction == "inbound")
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count())
                .select_from(Message)
                .where(Message.direction == "outbound")
            )
            == 0
        )
