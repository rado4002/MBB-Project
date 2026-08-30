from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from fastapi import Response
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.api.browser_auth_deps import BrowserPrincipal, BrowserSessionContext
from app.api.v1.operator_conversations import get_operator_conversation
from app.ai.audit import AITurnOutcome
from app.ai.commercial_state import (
    CommercialState,
    NextObjective,
    PurchaseIntent,
    read_commercial_state,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import (
    AITurn,
    AITurnExecutionError,
    AITurnPersistenceError,
    AITurnService,
)
from app.config import Settings
from app.models.ai_turn_audit import AITurnAudit
from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.inventory import InventoryRecord
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.pricing import SellableItemPrice
from app.modules.m1_gateway.service import process_inbound
from app.modules.m4_conversation.ownership import transition_ownership
from app.operator_identity.browser_auth import BrowserAuthState
from app.tasks.m1 import _handle_voice_note

DATABASE_URL = os.environ.get("AI4E_TEST_DATABASE_URL")
IDEMPOTENCY_SECRET = "ai4e-return-secret-" + ("x" * 32)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI4E_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)

TRUNCATE = text(
    """
    TRUNCATE TABLE
        mbb.conversation_ownership_idempotency,
        mbb.operator_audit_security_metadata,
        mbb.operator_audit_events,
        mbb.ai_turn_audits,
        mbb.escalation_tickets,
        mbb.messages,
        mbb.conversations,
        mbb.customers,
        mbb.inventory_statuses,
        mbb.sellable_item_prices,
        mbb.sellable_items,
        mbb.products,
        mbb.operator_accounts
    RESTART IDENTITY CASCADE
    """
)


class _SequenceAdapter:
    def __init__(self, *results: ProviderTurnResult) -> None:
        self.results = list(results)
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        return self.results.pop(0)


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
    inventory_status: str = "available",
    active: bool = True,
    with_price: bool = True,
) -> tuple[async_sessionmaker, Conversation, SellableItem, Message, OperatorAccount]:
    now = datetime(2026, 8, 29, 10, tzinfo=timezone.utc)
    product = Product(
        product_id=uuid.uuid4(),
        name="Air Fryer",
        category_code="air_fryer",
        description="AI-4E deterministic fixture.",
        active=active,
        created_at=now,
        updated_at=now,
    )
    item = SellableItem(
        sellable_item_id=uuid.uuid4(),
        product_id=product.product_id,
        model_label="6L",
        sku=f"AI4E-{uuid.uuid4().hex[:8].upper()}",
        attributes={"capacity_l": 6},
        active=active,
        created_at=now,
        updated_at=now,
    )
    customer = Customer(
        phone_number="+243810004001",
        name="AI-4E Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    initial_state = CommercialState(
        revision=3,
        current_goal="Family cooking",
        expressed_needs=["Family of 5"],
        purchase_intent=PurchaseIntent.considering,
        next_objective=NextObjective.prepare_handoff,
        selected_sellable_item_ids=[item.sellable_item_id],
        updated_at=now,
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="qualifying",
        language_detected="french",
        context={"commercial_state": initial_state.model_dump(mode="json")},
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
        content="Je prends celui-là.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"ai4e-{uuid.uuid4()}",
    )
    operator = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized=f"ai4e.{uuid.uuid4().hex[:8]}",
        display_name="AI-4E Operator",
        email_normalized=None,
        password_hash="not-used",
        role="operator",
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )
    records = [product, item, customer, conversation, inbound, operator]
    if with_price:
        records.append(
            SellableItemPrice(
                price_id=uuid.uuid4(),
                sellable_item_id=item.sellable_item_id,
                amount=Decimal("55.00"),
                currency="USD",
                effective_at=now,
                ended_at=None,
            )
        )
    records.append(
        InventoryRecord(
            inventory_id=uuid.uuid4(),
            sellable_item_id=item.sellable_item_id,
            status=inventory_status,
            updated_at=now,
        )
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(records)
        await session.commit()
    return factory, conversation, item, inbound, operator


def _handoff_result(
    *,
    reason: str,
    item_id: uuid.UUID | None = None,
    purchase_intent: str | None = None,
) -> ProviderTurnResult:
    arguments: dict[str, str] = {"reason_category": reason}
    if item_id is not None:
        arguments["selected_sellable_item_id"] = str(item_id)
    if purchase_intent is not None:
        arguments["purchase_intent"] = purchase_intent
    return ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id=f"handoff-{uuid.uuid4()}",
                capability_name="request_human_handoff",
                arguments=arguments,
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )


def _normal_finalizer(text_value: str) -> ProviderTurnResult:
    return ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id=f"final-{uuid.uuid4()}",
                capability_name="propose_commercial_state_update",
                arguments={
                    "response_text": text_value,
                    "state_update": {"next_objective": "retrieve_options"},
                },
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )


def _service(factory: async_sessionmaker, adapter, *, audit_appender=None):
    async def load(conversation_id: uuid.UUID):
        async with factory() as session:
            return await read_commercial_state(session, conversation_id)

    async def authority(_context):
        return True

    kwargs = {}
    if audit_appender is not None:
        kwargs["audit_appender"] = audit_appender
    return AITurnService(
        adapter,
        authority_checker=authority,
        durable_session_factory=factory,
        commercial_state_loader=load,
        **kwargs,
    )


def _turn(conversation: Conversation, source: Message, *, version: int = 1) -> AITurn:
    return AITurn(
        user_content=source.content,
        language="french",
        expected_ownership_version=version,
        conversation_id=conversation.conversation_id,
        source_message_id=source.message_id,
        allowed_capabilities=(
            "get_product_details",
            "request_human_handoff",
            "search_products",
        ),
    )


def _principal(operator: OperatorAccount) -> BrowserPrincipal:
    state = BrowserAuthState(
        redis_client=object(),
        settings=Settings(
            browser_session_hmac_secret="s" * 32,
            browser_csrf_hmac_secret="c" * 32,
        ),
    )
    return BrowserPrincipal(
        account=operator,
        session=BrowserSessionContext(
            raw_token="not-used",
            record=object(),
            state=state,
        ),
        capabilities=frozenset({"conversation.read"}),
    )


async def _operator_detail(factory, conversation_id, operator):
    async with factory() as session:
        return await get_operator_conversation(
            conversation_id=conversation_id,
            response=Response(),
            principal=_principal(operator),
            db=session,
        )


@pytest.mark.asyncio
async def test_qualified_handoff_persists_one_atomic_terminal_transition(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.modules.product_offer import service as product_offer_service

    factory, conversation, item, inbound, _operator = await _seed(engine)
    refreshes = 0
    original_require = product_offer_service.require_product_offer

    async def counted_require(*args, **kwargs):
        nonlocal refreshes
        refreshes += 1
        return await original_require(*args, **kwargs)

    monkeypatch.setattr(product_offer_service, "require_product_offer", counted_require)
    service = _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    )

    finalized = await service.generate_finalized(_turn(conversation, inbound))

    assert finalized.audit_persisted is True
    assert refreshes == 1
    assert finalized.outbound_message_id is not None
    assert finalized.text is not None and "Rien n'est encore confirmé" in finalized.text
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        state = await read_commercial_state(session, conversation.conversation_id)
        ticket = await session.scalar(
            select(EscalationTicket).where(
                EscalationTicket.conversation_id == conversation.conversation_id
            )
        )
        outbound = await session.get(Message, finalized.outbound_message_id)
        audit = await session.scalar(
            select(AITurnAudit).where(
                AITurnAudit.conversation_id == conversation.conversation_id
            )
        )
        assert persisted is not None and persisted.ai_execution_state == "paused"
        assert persisted.ownership_version == 2
        assert state is not None and state.purchase_intent is PurchaseIntent.ready
        assert state.next_objective is NextObjective.human_commercial_continuation
        assert state.selected_sellable_item_ids == [item.sellable_item_id]
        assert ticket is not None and ticket.reason == "qualified_purchase_intent"
        assert outbound is not None and outbound.content == finalized.text
        assert audit is not None and audit.outbound_message_id == outbound.message_id
        assert audit.outcome == AITurnOutcome.handoff_requested.value
        assert audit.commercial_state_revision_before == 3
        assert audit.commercial_state_revision_after == 4
        assert set(audit.commercial_state_changed_fields) == {
            "purchase_intent",
            "next_objective",
        }
        assert audit.capability_activity[0]["handoff_reason"] == (
            "qualified_purchase_intent"
        )


@pytest.mark.asyncio
async def test_failed_terminal_audit_rolls_back_objective_ack_state_ticket_and_pause(
    engine: AsyncEngine,
) -> None:
    factory, conversation, item, inbound, _operator = await _seed(engine)

    async def fail_audit(_session, _record):
        raise RuntimeError("deterministic audit failure")

    service = _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
        audit_appender=fail_audit,
    )
    with pytest.raises(AITurnPersistenceError):
        await service.generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        state = await read_commercial_state(session, conversation.conversation_id)
        assert persisted is not None and persisted.ai_execution_state == "eligible"
        assert persisted.ownership_version == 1
        assert state is not None and state.purchase_intent is PurchaseIntent.considering
        assert state.next_objective is NextObjective.prepare_handoff
        assert await session.scalar(select(func.count()).select_from(Message)) == 1
        assert await session.scalar(select(func.count()).select_from(EscalationTicket)) == 0
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inventory_status", "active", "with_price", "safe_code"),
    (
        ("out_of_stock", True, True, "out_of_stock"),
        ("available", False, True, "inactive"),
        ("unknown", True, True, "availability_unconfirmed"),
        ("available", True, False, "price_unavailable"),
    ),
)
async def test_non_actionable_or_unverified_offer_does_not_promote_or_handoff(
    engine: AsyncEngine,
    inventory_status: str,
    active: bool,
    with_price: bool,
    safe_code: str,
) -> None:
    factory, conversation, item, inbound, _operator = await _seed(
        engine,
        inventory_status=inventory_status,
        active=active,
        with_price=with_price,
    )
    adapter = _SequenceAdapter(
        _handoff_result(
            reason="qualified_purchase_intent",
            item_id=item.sellable_item_id,
            purchase_intent="ready",
        ),
        _normal_finalizer("Je vérifie une autre option disponible."),
    )

    finalized = await _service(factory, adapter).generate_finalized(
        _turn(conversation, inbound)
    )

    assert finalized.audit_record.outcome is AITurnOutcome.response_generated
    assert finalized.audit_record.capability_activity[0].safe_code == safe_code
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        state = await read_commercial_state(session, conversation.conversation_id)
        assert persisted is not None and persisted.ai_execution_state == "eligible"
        assert state is not None and state.purchase_intent is PurchaseIntent.considering
        assert state.next_objective is NextObjective.prepare_handoff
        assert await session.scalar(select(func.count()).select_from(EscalationTicket)) == 0
        assert await session.scalar(select(func.count()).select_from(Message)) == 1


@pytest.mark.asyncio
async def test_missing_selected_item_fails_closed_without_ready_or_handoff(
    engine: AsyncEngine,
) -> None:
    factory, conversation, _item, inbound, _operator = await _seed(engine)
    adapter = _SequenceAdapter(
        _handoff_result(
            reason="qualified_purchase_intent",
            item_id=uuid.uuid4(),
            purchase_intent="ready",
        ),
        _normal_finalizer("Je dois d'abord retrouver le produit exact."),
    )

    finalized = await _service(factory, adapter).generate_finalized(
        _turn(conversation, inbound)
    )

    assert finalized.audit_record.capability_activity[0].safe_code == (
        "sellable_item_not_found"
    )
    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        assert state is not None and state.purchase_intent is PurchaseIntent.considering
        assert await session.scalar(select(func.count()).select_from(EscalationTicket)) == 0


@pytest.mark.asyncio
async def test_duplicate_terminal_result_is_stale_and_creates_no_second_ack(
    engine: AsyncEngine,
) -> None:
    factory, conversation, item, inbound, _operator = await _seed(engine)

    def service():
        return _service(
            factory,
            _SequenceAdapter(
                _handoff_result(
                    reason="qualified_purchase_intent",
                    item_id=item.sellable_item_id,
                    purchase_intent="ready",
                )
            ),
        )

    await service().generate_finalized(_turn(conversation, inbound))
    with pytest.raises(AITurnExecutionError) as stale:
        await service().generate_finalized(_turn(conversation, inbound))
    assert stale.value.audit_record.safe_code == "stale_ai_authority"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(EscalationTicket)) == 1
        assert await session.scalar(
            select(func.count()).select_from(Message).where(Message.direction == "outbound")
        ) == 1
        assert await session.scalar(select(func.count()).select_from(AITurnAudit)) == 1


@pytest.mark.asyncio
async def test_change_of_mind_persists_while_ai_and_handoff_remain_paused(
    engine: AsyncEngine,
) -> None:
    factory, conversation, item, inbound, _operator = await _seed(engine)
    await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))

    change_message_id = uuid.uuid4()
    async with factory() as session:
        persisted_inbound = await process_inbound(
            session=session,
            customer_phone=conversation.customer_id,
            content="Actually never mind.",
            content_type="text",
            timestamp=datetime.now(timezone.utc),
            whatsapp_message_id=f"ai4e-change-{uuid.uuid4()}",
            message_id=change_message_id,
        )
        await session.commit()
    assert persisted_inbound.message_id == change_message_id

    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        newest = await session.get(Message, change_message_id)
        ticket = await session.scalar(select(EscalationTicket))
        assert persisted is not None and persisted.ai_execution_state == "paused"
        assert persisted.owner_type == "ai" and persisted.human_owner_account_id is None
        assert newest is not None and newest.content == "Actually never mind."
        assert ticket is not None and ticket.status == "open"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reason", "purchase_transition", "expected_intent"),
    (
        ("explicit_human_request", None, PurchaseIntent.considering),
        ("authority_required", "considering", PurchaseIntent.considering),
        ("reliability_tool_failure", None, PurchaseIntent.considering),
    ),
)
async def test_other_canonical_reasons_use_terminal_objective_without_false_ready(
    engine: AsyncEngine,
    reason: str,
    purchase_transition: str | None,
    expected_intent: PurchaseIntent,
) -> None:
    factory, conversation, _item, inbound, _operator = await _seed(engine)
    finalized = await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(reason=reason, purchase_intent=purchase_transition)
        ),
    ).generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        ticket = await session.scalar(select(EscalationTicket))
        assert state is not None and state.purchase_intent is expected_intent
        assert state.next_objective is NextObjective.human_commercial_continuation
        assert ticket is not None and ticket.reason == reason
        assert finalized.outbound_message_id is not None


@pytest.mark.asyncio
async def test_explicit_human_request_ignores_irrelevant_stale_selected_item(
    engine: AsyncEngine,
) -> None:
    factory, conversation, _item, inbound, _operator = await _seed(engine)
    finalized = await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="explicit_human_request",
                item_id=uuid.uuid4(),
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        ticket = await session.scalar(select(EscalationTicket))
        assert state is not None and state.purchase_intent is PurchaseIntent.considering
        assert state.next_objective is NextObjective.human_commercial_continuation
        assert ticket is not None and ticket.reason == "explicit_human_request"
        assert finalized.outbound_message_id is not None


@pytest.mark.asyncio
async def test_return_to_ai_requires_fresh_current_message_for_a_second_handoff(
    engine: AsyncEngine,
) -> None:
    factory, conversation, item, inbound, operator = await _seed(engine)
    await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        takeover = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="human",
            expected_version=2,
            actor_account_id=operator.account_id,
            actor_display_name=operator.display_name,
            actor_role=operator.role,
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai4e-takeover",
            ai_adapter="claude",
        )
        returned = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="ai",
            expected_version=takeover.ownership.version,
            actor_account_id=operator.account_id,
            actor_display_name=operator.display_name,
            actor_role=operator.role,
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai4e-return",
            ai_adapter="claude",
        )
        assert returned.ownership.version == 4

    exploration = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=datetime(2026, 8, 29, 10, 5, tzinfo=timezone.utc),
        direction="inbound",
        content="Montre-moi une autre option.",
        content_type="text",
        language="french",
        whatsapp_message_id="ai4e-return-explore",
    )
    async with factory() as session:
        session.add(exploration)
        await session.commit()
    continued = await _service(
        factory,
        _SequenceAdapter(_normal_finalizer("Voici une autre option.")),
    ).generate_finalized(_turn(conversation, exploration, version=4))
    assert continued.audit_record.outcome is AITurnOutcome.response_generated

    fresh = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=datetime(2026, 8, 29, 10, 6, tzinfo=timezone.utc),
        direction="inbound",
        content="Je prends finalement le 6L.",
        content_type="text",
        language="french",
        whatsapp_message_id="ai4e-return-fresh",
    )
    async with factory() as session:
        session.add(fresh)
        await session.commit()
    second = await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, fresh, version=4))
    assert second.audit_record.outcome is AITurnOutcome.handoff_requested
    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(EscalationTicket)
        ) == 2
        assert await session.scalar(
            select(func.count())
            .select_from(EscalationTicket)
            .where(EscalationTicket.status.in_(("open", "in_progress")))
        ) == 1


@pytest.mark.asyncio
async def test_postcommit_delivery_reuses_persisted_outbound_uuid(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.adapters as adapters
    import app.database as database
    from app.tasks import m1

    factory, conversation, item, inbound, _operator = await _seed(engine)
    finalized = await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))
    assert finalized.text is not None and finalized.outbound_message_id is not None

    class _Messaging:
        calls: list[tuple[str, str, str | None]] = []

        async def send_message(self, phone, content, *, idempotency_key=None):
            self.calls.append((phone, content, idempotency_key))
            return "scripted-provider-id"

    messaging = _Messaging()
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(m1.settings, "whatsapp_send_enabled", True)
    monkeypatch.setattr(adapters, "get_messaging_adapter", lambda: messaging)

    result = await m1._send_persisted_handoff_ack_safe(
        conversation.customer_id,
        finalized.text,
        outbound_message_id=finalized.outbound_message_id,
        conversation_id=conversation.conversation_id,
    )
    tampered = await m1._send_persisted_handoff_ack_safe(
        conversation.customer_id,
        "different text",
        outbound_message_id=finalized.outbound_message_id,
        conversation_id=conversation.conversation_id,
    )

    assert result == {
        "status": "sent",
        "provider_message_id": "scripted-provider-id",
    }
    assert messaging.calls == [
        (
            conversation.customer_id,
            finalized.text,
            str(finalized.outbound_message_id),
        )
    ]
    assert tampered == {"status": "unknown_or_failed"}


@pytest.mark.asyncio
async def test_operator_projection_reads_live_product_and_ai_provenance(
    engine: AsyncEngine,
) -> None:
    from app.api.v1.operator_conversations import (
        _ai_actor_display_name,
        _commercial_context_response,
        _operator_message_item,
    )

    factory, conversation, item, inbound, _operator = await _seed(engine)
    finalized = await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))
    assert finalized.outbound_message_id is not None

    async with factory() as session:
        state = await read_commercial_state(session, conversation.conversation_id)
        assert state is not None
        context = await _commercial_context_response(
            session,
            state.model_dump(mode="json"),
        )
        ai_display = _ai_actor_display_name()
        row = (
            await session.execute(
                select(
                    Message,
                    ai_display.label("ai_actor_display_name"),
                ).where(Message.message_id == finalized.outbound_message_id)
            )
        ).one()

    assert context is not None
    assert context.selected_products[0].display_name == "Air Fryer 6L"
    assert context.selected_products[0].offer_status == "sellable_now"
    projected = _operator_message_item(
        message_id=row.Message.message_id,
        occurred_at=row.Message.timestamp,
        direction=row.Message.direction,
        content_type=row.Message.content_type,
        content=row.Message.content,
        language=row.Message.language,
        operator_author_account_id=None,
        author_display_name=None,
        delivery_state=None,
        delivery_state_timestamp=None,
        ai_actor_display_name=row.ai_actor_display_name,
    )
    assert projected.sender_type == "ai"
    assert projected.sender_display_name == "MBB AI Assistant"


@pytest.mark.asyncio
async def test_legacy_voice_ticket_keeps_provenance_while_operator_reads_current_ai_reason(
    engine: AsyncEngine,
) -> None:
    factory, conversation, item, inbound, operator = await _seed(engine)
    async with factory() as session:
        await _handle_voice_note(
            session=session,
            customer_phone=conversation.customer_id,
            conversation_id=conversation.conversation_id,
            language=conversation.language_detected,
        )

    await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        tickets = (
            await session.execute(
                select(EscalationTicket).where(
                    EscalationTicket.conversation_id == conversation.conversation_id,
                    EscalationTicket.status.in_(("open", "in_progress")),
                )
            )
        ).scalars().all()
        audit = await session.scalar(
            select(AITurnAudit).where(
                AITurnAudit.conversation_id == conversation.conversation_id,
                AITurnAudit.outcome == AITurnOutcome.handoff_requested.value,
            )
        )
        persisted = await session.get(Conversation, conversation.conversation_id)

    detail = await _operator_detail(factory, conversation.conversation_id, operator)
    assert len(tickets) == 1
    assert tickets[0].reason == "voice_note"
    assert tickets[0].source == "legacy"
    assert audit is not None
    assert audit.capability_activity[0]["handoff_reason"] == (
        "qualified_purchase_intent"
    )
    assert persisted is not None and persisted.ai_execution_state == "paused"
    assert detail.open_escalation.reason == "qualified_purchase_intent"


@pytest.mark.asyncio
async def test_operator_ticket_keeps_provenance_while_current_ai_reason_is_effective(
    engine: AsyncEngine,
) -> None:
    factory, conversation, _item, inbound, operator = await _seed(engine)
    async with factory() as session:
        session.add(
            EscalationTicket(
                conversation_id=conversation.conversation_id,
                customer_id=conversation.customer_id,
                priority="high",
                reason="complex_complaint",
                source="operator_browser",
                escalation_type="complex_issue",
                operator_reason="Customer complaint requires operator review.",
                created_by_account_id=operator.account_id,
                status="open",
                transcript_snapshot=[],
            )
        )
        await session.commit()

    await _service(
        factory,
        _SequenceAdapter(_handoff_result(reason="explicit_human_request")),
    ).generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        tickets = (
            await session.execute(
                select(EscalationTicket).where(
                    EscalationTicket.conversation_id == conversation.conversation_id,
                    EscalationTicket.status.in_(("open", "in_progress")),
                )
            )
        ).scalars().all()
    detail = await _operator_detail(factory, conversation.conversation_id, operator)

    assert len(tickets) == 1
    assert tickets[0].reason == "complex_complaint"
    assert tickets[0].source == "operator_browser"
    assert tickets[0].operator_reason == (
        "Customer complaint requires operator review."
    )
    assert tickets[0].created_by_account_id == operator.account_id
    assert detail.open_escalation.reason == "explicit_human_request"


@pytest.mark.asyncio
async def test_active_legacy_ticket_without_current_terminal_audit_uses_original_reason(
    engine: AsyncEngine,
) -> None:
    factory, conversation, _item, _inbound, operator = await _seed(engine)
    async with factory() as session:
        await _handle_voice_note(
            session=session,
            customer_phone=conversation.customer_id,
            conversation_id=conversation.conversation_id,
            language=conversation.language_detected,
        )

    detail = await _operator_detail(factory, conversation.conversation_id, operator)
    assert detail.ownership.ai_execution_state == "eligible"
    assert detail.open_escalation.reason == "voice_note"


@pytest.mark.asyncio
async def test_return_to_ai_and_later_escalation_do_not_expose_stale_terminal_reason(
    engine: AsyncEngine,
) -> None:
    factory, conversation, item, inbound, operator = await _seed(engine)
    await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason="qualified_purchase_intent",
                item_id=item.sellable_item_id,
                purchase_intent="ready",
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))

    async with factory() as session:
        takeover = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="human",
            expected_version=2,
            actor_account_id=operator.account_id,
            actor_display_name=operator.display_name,
            actor_role=operator.role,
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai4f-takeover",
            ai_adapter="claude",
        )
        returned = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="ai",
            expected_version=takeover.ownership.version,
            actor_account_id=operator.account_id,
            actor_display_name=operator.display_name,
            actor_role=operator.role,
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai4f-return",
            ai_adapter="claude",
        )
    assert returned.ownership.version == 4

    returned_detail = await _operator_detail(
        factory,
        conversation.conversation_id,
        operator,
    )
    assert returned_detail.ownership.ai_execution_state == "eligible"
    assert returned_detail.open_escalation.exists is False
    assert returned_detail.open_escalation.reason is None

    async with factory() as session:
        session.add(
            EscalationTicket(
                conversation_id=conversation.conversation_id,
                customer_id=conversation.customer_id,
                priority="high",
                reason="voice_note",
                source="legacy",
                status="open",
                transcript_snapshot=[],
            )
        )
        await session.commit()

    later_detail = await _operator_detail(
        factory,
        conversation.conversation_id,
        operator,
    )
    assert later_detail.open_escalation.exists is True
    assert later_detail.open_escalation.reason == "voice_note"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason",
    (
        "qualified_purchase_intent",
        "explicit_human_request",
        "authority_required",
        "reliability_tool_failure",
    ),
)
async def test_clean_current_ai4e_handoff_projects_each_canonical_reason(
    engine: AsyncEngine,
    reason: str,
) -> None:
    factory, conversation, item, inbound, operator = await _seed(engine)
    await _service(
        factory,
        _SequenceAdapter(
            _handoff_result(
                reason=reason,
                item_id=(
                    item.sellable_item_id
                    if reason == "qualified_purchase_intent"
                    else None
                ),
                purchase_intent=(
                    "ready"
                    if reason == "qualified_purchase_intent"
                    else "considering" if reason == "authority_required" else None
                ),
            )
        ),
    ).generate_finalized(_turn(conversation, inbound))

    detail = await _operator_detail(factory, conversation.conversation_id, operator)
    assert detail.open_escalation.exists is True
    assert detail.open_escalation.reason == reason
