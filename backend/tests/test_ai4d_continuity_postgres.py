from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ai.audit import AITurnAuditRecord, AITurnOutcome
from app.ai.commercial_state import (
    CommercialState,
    CommercialStateUpdate,
    PurchaseIntent,
    read_commercial_state,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService
from app.models.ai_turn_audit import AITurnAudit
from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.modules.m4_conversation.ownership import ai_may_reply

DATABASE_URL = os.environ.get("AI4D_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI4D_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)

TRUNCATE = text(
    """
    TRUNCATE TABLE
        mbb.ai_turn_audits,
        mbb.messages,
        mbb.conversations,
        mbb.customers,
        mbb.inventory_statuses,
        mbb.sellable_item_prices,
        mbb.product_media,
        mbb.sellable_items,
        mbb.products
    RESTART IDENTITY CASCADE
    """
)


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL, pool_size=5)
    async with database_engine.begin() as connection:
        await connection.execute(TRUNCATE)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await database_engine.dispose()


async def _seed(
    engine: AsyncEngine,
    *,
    state: CommercialState | None = None,
    legacy_state: dict | None = None,
) -> tuple[async_sessionmaker, Conversation, Message, tuple[SellableItem, ...]]:
    now = datetime(2026, 8, 29, 4, tzinfo=timezone.utc)
    customer = Customer(
        phone_number="+243810000095",
        name="AI4D Fictional Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    context = {"qualification_state": {"step": "q2_location"}}
    if state is not None:
        context["commercial_state"] = state.model_dump(mode="json")
    if legacy_state is not None:
        context["commercial_state"] = legacy_state
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="active",
        language_detected="french",
        context=context,
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
        created_at=now,
        direction="inbound",
        content="Je cherche un air fryer.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"ai4d-{uuid.uuid4()}",
    )
    product = Product(
        product_id=uuid.uuid4(),
        name="Fictional Air Fryer",
        category_code="air_fryer",
        description="Fictional product for deterministic AI4D validation.",
        active=True,
    )
    items = tuple(
        SellableItem(
            sellable_item_id=uuid.uuid4(),
            product_id=product.product_id,
            model_label=f"{capacity}L",
            sku=f"AI4D-{capacity}L",
            attributes={"capacity_l": capacity},
            active=capacity != 8,
        )
        for capacity in (4, 6, 8, 10)
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all((customer, conversation, inbound, product, *items))
        await session.commit()
    return factory, conversation, inbound, items


def _audit(
    conversation: Conversation,
    source: Message,
    *,
    revision: int,
) -> AITurnAuditRecord:
    return AITurnAuditRecord(
        turn_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        source_message_id=source.message_id,
        policy_version=AI_SYSTEM_POLICY_VERSION,
        provider="scripted",
        model="offline-fixture",
        commercial_state_revision_before=revision,
        commercial_state_revision_after=revision,
        outcome=AITurnOutcome.response_generated,
    )


async def _persist(
    monkeypatch,
    factory,
    conversation: Conversation,
    source: Message,
    *,
    revision: int,
    state_update: CommercialStateUpdate | None,
) -> uuid.UUID | None:
    import app.database as database
    from app.tasks import m1

    monkeypatch.setattr(database, "async_session_factory", factory)
    return await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content="Réponse commerciale fictive.",
        language="french",
        processing_time_ms=12,
        expected_ownership_version=conversation.ownership_version,
        source_message_id=source.message_id,
        audit_record=_audit(conversation, source, revision=revision),
        expected_commercial_state_revision=revision,
        commercial_state_update=state_update,
    )


@pytest.mark.asyncio
async def test_turn_snapshot_finalizer_and_m1_persistence_form_one_durable_slice(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    import app.database as database
    from app.tasks import m1

    factory, conversation, source, items = await _seed(engine)
    monkeypatch.setattr(database, "async_session_factory", factory)

    class _FinalizerAdapter:
        def __init__(self):
            self.calls = []

        async def generate_turn(self, request):
            self.calls.append(request)
            return ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="ai4d-final",
                        capability_name="propose_commercial_state_update",
                        arguments={
                            "response_text": "Je garde le modèle 6L pour la suite.",
                            "state_update": {
                                "current_goal": "find an air fryer",
                                "selected_sellable_item_ids": [
                                    str(items[1].sellable_item_id)
                                ],
                            },
                        },
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
            )

    async def load_state(conversation_id):
        async with factory() as session:
            return await read_commercial_state(session, conversation_id)

    async def authority(context):
        async with factory() as session:
            return await ai_may_reply(
                session,
                context.conversation_id,
                expected_ownership_version=context.expected_ownership_version,
            )

    adapter = _FinalizerAdapter()
    finalized = await AITurnService(
        adapter,
        authority_checker=authority,
        commercial_state_loader=load_state,
    ).generate_finalized(
        AITurn(
            user_content="Je choisis le 6L.",
            language="french",
            expected_ownership_version=1,
            conversation_id=conversation.conversation_id,
            source_message_id=source.message_id,
        )
    )
    outbound_id = await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content=finalized.text,
        language="french",
        processing_time_ms=8,
        expected_ownership_version=1,
        source_message_id=source.message_id,
        audit_record=finalized.audit_record,
        expected_commercial_state_revision=(
            finalized.commercial_state_snapshot_revision
        ),
        commercial_state_update=finalized.commercial_state_update,
    )

    assert outbound_id is not None
    assert len(adapter.calls) == 1
    assert [capability.name for capability in adapter.calls[0].allowed_capabilities] == [
        "propose_commercial_state_update"
    ]
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        audit = await session.scalar(select(AITurnAudit))
    assert state is not None
    assert state.revision == 1
    assert state.selected_sellable_item_ids == [items[1].sellable_item_id]
    assert audit is not None
    assert audit.outbound_message_id == outbound_id
    assert audit.commercial_state_revision_before == 0
    assert audit.commercial_state_revision_after == 1


@pytest.mark.asyncio
async def test_state_outbound_and_audit_commit_atomically_with_selected_id_validation(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory, conversation, source, items = await _seed(engine)
    outbound_id = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=0,
        state_update=CommercialStateUpdate(
            current_goal="find an air fryer",
            decision_constraints=[{"kind": "budget", "value": "maximum $60"}],
            selected_sellable_item_ids=[str(items[1].sellable_item_id)],
        ),
    )
    assert outbound_id is not None

    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        stored_context = await session.scalar(
            select(Conversation.context).where(
                Conversation.conversation_id == conversation.conversation_id
            )
        )
        audits = (
            await session.scalars(
                select(AITurnAudit).where(
                    AITurnAudit.conversation_id == conversation.conversation_id
                )
            )
        ).all()
        outbound_count = await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.conversation_id == conversation.conversation_id,
                Message.direction == "outbound",
            )
        )
    assert state is not None
    assert state.schema_version == 2
    assert state.revision == 1
    assert state.updated_at is not None and state.updated_at.utcoffset() == timedelta(0)
    assert state.selected_sellable_item_ids == [items[1].sellable_item_id]
    assert stored_context["qualification_state"] == {"step": "q2_location"}
    assert outbound_count == 1
    assert len(audits) == 1
    assert audits[0].outbound_message_id == outbound_id
    assert audits[0].commercial_state_revision_before == 0
    assert audits[0].commercial_state_revision_after == 1
    assert audits[0].commercial_state_changed_fields == [
        "current_goal",
        "decision_constraints",
        "selected_sellable_item_ids",
    ]


@pytest.mark.asyncio
async def test_inactive_known_item_is_accepted_but_unknown_item_rolls_back_everything(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory, conversation, source, items = await _seed(engine)
    inactive_outbound = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=0,
        state_update=CommercialStateUpdate(
            selected_sellable_item_ids=[str(items[2].sellable_item_id)]
        ),
    )
    assert inactive_outbound is not None

    unknown = uuid.uuid4()
    failed = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=1,
        state_update=CommercialStateUpdate(
            selected_sellable_item_ids=[str(unknown)]
        ),
    )
    assert failed is None
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        outbound_count = await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.direction == "outbound"
            )
        )
        audit_count = await session.scalar(select(func.count()).select_from(AITurnAudit))
    assert state is not None
    assert state.revision == 1
    assert state.selected_sellable_item_ids == [items[2].sellable_item_id]
    assert outbound_count == 1
    assert audit_count == 1


@pytest.mark.asyncio
async def test_no_op_does_not_rewrite_jsonb_increment_revision_or_updated_at(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    initial = CommercialState(
        revision=3,
        current_goal="find an air fryer",
        updated_at=datetime(2026, 8, 28, 1, tzinfo=timezone.utc),
    )
    factory, conversation, source, _items = await _seed(engine, state=initial)
    outbound_id = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=3,
        state_update=CommercialStateUpdate(current_goal="find an air fryer"),
    )
    assert outbound_id is not None
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        audit = await session.scalar(select(AITurnAudit))
    assert state == initial
    assert audit is not None
    assert audit.commercial_state_revision_before == 3
    assert audit.commercial_state_revision_after == 3
    assert audit.commercial_state_changed_fields == []


@pytest.mark.asyncio
async def test_stale_revision_ownership_and_newer_message_each_fail_closed(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    initial = CommercialState(revision=2, current_goal="find an air fryer")
    factory, conversation, source, _items = await _seed(engine, state=initial)

    stale_revision = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=1,
        state_update=CommercialStateUpdate(expressed_needs=["serves five"]),
    )
    assert stale_revision is None

    conversation.ownership_version = 1
    async with factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation.conversation_id)
            .values(ownership_version=2)
        )
        await session.commit()
    stale_ownership = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=2,
        state_update=CommercialStateUpdate(expressed_needs=["serves five"]),
    )
    assert stale_ownership is None

    async with factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation.conversation_id)
            .values(ownership_version=1)
        )
        newer = Message(
            message_id=uuid.uuid4(),
            conversation_id=conversation.conversation_id,
            timestamp=source.timestamp + timedelta(seconds=1),
            created_at=source.created_at + timedelta(seconds=1),
            direction="inbound",
            content="Pour cinq personnes.",
            content_type="text",
            language="french",
            whatsapp_message_id=f"ai4d-newer-{uuid.uuid4()}",
        )
        session.add(newer)
        await session.commit()
    stale_source = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=2,
        state_update=CommercialStateUpdate(expressed_needs=["serves five"]),
    )
    assert stale_source is None
    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.direction == "outbound"
            )
        ) == 0
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 0
        state = await read_commercial_state(session, conversation.conversation_id)
    assert state == initial


@pytest.mark.asyncio
async def test_rapid_multi_message_burst_suppresses_older_inference_without_merge(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    factory, conversation, message_a, _items = await _seed(engine)
    async with factory() as session:
        message_b = Message(
            message_id=uuid.uuid4(),
            conversation_id=conversation.conversation_id,
            timestamp=message_a.timestamp + timedelta(seconds=1),
            created_at=message_a.created_at + timedelta(seconds=1),
            direction="inbound",
            content="Pour 5 personnes.",
            content_type="text",
            language="french",
            whatsapp_message_id=f"ai4d-b-{uuid.uuid4()}",
        )
        message_c = Message(
            message_id=uuid.uuid4(),
            conversation_id=conversation.conversation_id,
            timestamp=message_a.timestamp + timedelta(seconds=2),
            created_at=message_a.created_at + timedelta(seconds=2),
            direction="inbound",
            content="Mon budget maximum est 60 USD.",
            content_type="text",
            language="french",
            whatsapp_message_id=f"ai4d-c-{uuid.uuid4()}",
        )
        session.add_all((message_b, message_c))
        await session.commit()

    result_a, result_c = await asyncio.gather(
        _persist(
            monkeypatch,
            factory,
            conversation,
            message_a,
            revision=0,
            state_update=CommercialStateUpdate(current_goal="find an air fryer"),
        ),
        _persist(
            monkeypatch,
            factory,
            conversation,
            message_c,
            revision=0,
            state_update=CommercialStateUpdate(
                current_goal="find an air fryer",
                expressed_needs=["serves five people"],
                decision_constraints=[
                    {"kind": "budget", "value": "maximum $60"}
                ],
            ),
        ),
    )
    assert result_a is None
    assert result_c is not None
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        outbound_count = await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.direction == "outbound"
            )
        )
    assert state is not None and state.revision == 1
    assert state.expressed_needs == ["serves five people"]
    assert [item.value for item in state.decision_constraints] == ["maximum $60"]
    assert outbound_count == 1


@pytest.mark.asyncio
async def test_human_period_return_to_ai_continuity_and_fresh_conversation_is_clean(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    initial = CommercialState(
        revision=1,
        current_goal="find an air fryer",
        expressed_needs=["serves five people"],
    )
    factory, conversation, source, _items = await _seed(engine, state=initial)
    human_time = source.created_at + timedelta(minutes=1)
    async with factory() as session:
        operator = OperatorAccount(
            account_id=uuid.uuid4(),
            username_normalized=f"ai4d.operator.{uuid.uuid4().hex[:8]}",
            display_name="AI4D Operator",
            email_normalized=None,
            password_hash="not-used",
            role="administrator",
            status="active",
            auth_version=1,
            must_change_password=False,
            temporary_password_expires_at=None,
            password_changed_at=human_time,
            last_login_at=None,
            created_at=human_time,
            updated_at=human_time,
        )
        session.add(operator)
        await session.flush()
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation.conversation_id)
            .values(
                owner_type="human",
                human_owner_account_id=operator.account_id,
                ai_execution_state="paused",
                ownership_version=2,
            )
        )
        await session.commit()
    async with factory() as session:
        human_period_state = await read_commercial_state(
            session, conversation.conversation_id
        )
        assert human_period_state == initial
        stored = await session.get(Conversation, conversation.conversation_id)
        assert stored is not None and stored.owner_type == "human"
        human_period_message = Message(
            message_id=uuid.uuid4(),
            conversation_id=conversation.conversation_id,
            timestamp=human_time,
            created_at=human_time,
            direction="inbound",
            content="Je préfère quelque chose de facile à nettoyer.",
            content_type="text",
            language="french",
            whatsapp_message_id=f"ai4d-human-{uuid.uuid4()}",
        )
        session.add(human_period_message)
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation.conversation_id)
            .values(
                owner_type="ai",
                human_owner_account_id=None,
                ai_execution_state="eligible",
                ownership_version=3,
            )
        )
        await session.commit()
    conversation.ownership_version = 3
    resumed = await _persist(
        monkeypatch,
        factory,
        conversation,
        human_period_message,
        revision=1,
        state_update=CommercialStateUpdate(
            expressed_needs=["serves five people", "easy to clean"]
        ),
    )
    assert resumed is not None
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        fresh_customer = Customer(
            phone_number="+243810000096",
            city="Kinshasa",
            preferred_language="french",
        )
        fresh_conversation = Conversation(
            conversation_id=uuid.uuid4(),
            customer_id=fresh_customer.phone_number,
            language_detected="french",
            context={},
        )
        session.add_all((fresh_customer, fresh_conversation))
        await session.commit()
        fresh_state = await read_commercial_state(
            session, fresh_conversation.conversation_id
        )
    assert state is not None
    assert state.expressed_needs == ["serves five people", "easy to clean"]
    assert fresh_state is None


@pytest.mark.asyncio
async def test_legacy_v1_evolves_only_on_meaningful_write_without_purchase_activation(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    legacy = {
        "schema_version": 1,
        "revision": 4,
        "current_goal": "find an air fryer",
        "expressed_needs": [],
        "decision_constraints": [{"kind": "budget", "value": "maximum $70"}],
        "open_questions": [],
        "current_concern": None,
        "purchase_intent": "considering",
        "next_objective": "retrieve_options",
    }
    factory, conversation, source, _items = await _seed(
        engine, legacy_state=legacy
    )
    outbound_id = await _persist(
        monkeypatch,
        factory,
        conversation,
        source,
        revision=4,
        state_update=CommercialStateUpdate(
            decision_constraints=[{"kind": "budget", "value": "maximum $50"}]
        ),
    )
    assert outbound_id is not None
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        context = await session.scalar(
            select(Conversation.context).where(
                Conversation.conversation_id == conversation.conversation_id
            )
        )
    assert state is not None
    assert state.schema_version == 2
    assert state.revision == 5
    assert state.purchase_intent is PurchaseIntent.considering
    assert context["commercial_state"]["purchase_intent"] == "considering"


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_state_and_outbound(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    import app.database as database
    from app.tasks import m1

    factory, conversation, source, _items = await _seed(engine)
    monkeypatch.setattr(database, "async_session_factory", factory)

    async def fail_audit(_session, _record):
        raise RuntimeError("fictional audit failure")

    monkeypatch.setattr(m1, "append_ai_turn_audit", fail_audit)
    persisted = await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content="Must roll back.",
        language="french",
        processing_time_ms=1,
        expected_ownership_version=1,
        source_message_id=source.message_id,
        audit_record=_audit(conversation, source, revision=0),
        expected_commercial_state_revision=0,
        commercial_state_update=CommercialStateUpdate(
            current_goal="find an air fryer"
        ),
    )
    assert persisted is None
    async with factory() as session:
        assert await read_commercial_state(session, conversation.conversation_id) is None
        assert await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.direction == "outbound"
            )
        ) == 0
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 0


@pytest.mark.asyncio
async def test_outbound_failure_rolls_back_state_and_audit(
    engine: AsyncEngine,
    monkeypatch,
) -> None:
    import app.database as database
    from app.modules.m1_gateway import service as m1_service
    from app.tasks import m1

    factory, conversation, source, _items = await _seed(engine)
    monkeypatch.setattr(database, "async_session_factory", factory)

    async def fail_outbound(**_kwargs):
        raise RuntimeError("fictional outbound failure")

    monkeypatch.setattr(m1_service, "persist_outbound", fail_outbound)
    persisted = await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content="Must roll back.",
        language="french",
        processing_time_ms=1,
        expected_ownership_version=1,
        source_message_id=source.message_id,
        audit_record=_audit(conversation, source, revision=0),
        expected_commercial_state_revision=0,
        commercial_state_update=CommercialStateUpdate(
            current_goal="find an air fryer"
        ),
    )
    assert persisted is None
    async with factory() as session:
        assert await read_commercial_state(session, conversation.conversation_id) is None
        assert await session.scalar(
            select(func.count()).select_from(Message).where(
                Message.direction == "outbound"
            )
        ) == 0
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 0
