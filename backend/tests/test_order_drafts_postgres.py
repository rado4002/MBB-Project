from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ai.commercial_state import CommercialStateUpdate, update_commercial_state
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService
from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.inventory import InventoryRecord
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.modules.m7_conversion.order_drafts import (
    StaleOrderDraftAuthority,
    handle_order_draft_reply,
    prepare_order_draft,
)

DATABASE_URL = os.environ.get("AI6B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI6B_TEST_DATABASE_URL is required for order-draft PostgreSQL evidence",
)

TRUNCATE = text(
    "TRUNCATE TABLE mbb.customers, mbb.products, mbb.exchange_rates "
    "RESTART IDENTITY CASCADE"
)


@dataclass(frozen=True)
class SeededJourney:
    conversation_id: uuid.UUID
    source_message_id: uuid.UUID
    sellable_item_id: uuid.UUID
    lead_id: uuid.UUID
    price_id: uuid.UUID
    inventory_id: uuid.UUID


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    value = create_async_engine(DATABASE_URL)
    async with value.begin() as connection:
        await connection.execute(TRUNCATE)
    try:
        yield value
    finally:
        async with value.begin() as connection:
            await connection.execute(TRUNCATE)
        await value.dispose()


async def _seed(factory) -> SeededJourney:
    now = datetime.now(timezone.utc)
    customer = Customer(
        phone_number="+243810006001",
        name="AI-6B Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        language_detected="french",
        context={},
        owner_type="ai",
        ai_execution_state="eligible",
        ownership_version=1,
        ownership_updated_at=now,
        start_time=now,
        last_message_time=now,
        created_at=now,
        updated_at=now,
    )
    product = Product(
        product_id=uuid.uuid4(),
        name="MBB Test Air Fryer",
        category_code="air_fryer",
        description="Synthetic AI-6B product.",
        active=True,
        created_at=now,
        updated_at=now,
    )
    item = SellableItem(
        sellable_item_id=uuid.uuid4(),
        product_id=product.product_id,
        model_label="6L",
        sku="AI6B-AIR-6L",
        attributes={"capacity_l": 6},
        active=True,
        created_at=now,
        updated_at=now,
    )
    price = SellableItemPrice(
        price_id=uuid.uuid4(),
        sellable_item_id=item.sellable_item_id,
        amount=Decimal("55.00"),
        currency="USD",
        effective_at=now,
        ended_at=None,
    )
    rate = ExchangeRate(
        exchange_rate_id=uuid.uuid4(),
        base_currency="USD",
        quote_currency="CDF",
        rate=Decimal("2800.000000"),
        effective_at=now,
        ended_at=None,
    )
    inventory = InventoryRecord(
        inventory_id=uuid.uuid4(),
        sellable_item_id=item.sellable_item_id,
        status="available",
        updated_at=now,
    )
    source = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=now,
        direction="inbound",
        content="Je le prends.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"ai6b-{uuid.uuid4()}",
        created_at=now,
    )
    lead = Lead(
        lead_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        conversation_id=conversation.conversation_id,
        score="hot",
        score_value=9,
        stage="decision",
        intent="product_inquiry",
        product_interest=["air_fryer"],
        source="whatsapp",
        relance_count=0,
        qualified_at=now,
        created_at=now,
        updated_at=now,
    )
    async with factory() as session:
        session.add_all(
            [
                customer,
                conversation,
                product,
                item,
                price,
                rate,
                inventory,
                source,
                lead,
            ]
        )
        await session.commit()
    return SeededJourney(
        conversation_id=conversation.conversation_id,
        source_message_id=source.message_id,
        sellable_item_id=item.sellable_item_id,
        lead_id=lead.lead_id,
        price_id=price.price_id,
        inventory_id=inventory.inventory_id,
    )


async def _prepare(factory, seeded: SeededJourney, *, quantity: int = 2):
    async with factory() as session:
        result = await prepare_order_draft(
            session,
            conversation_id=seeded.conversation_id,
            source_message_id=seeded.source_message_id,
            turn_id=uuid.uuid4(),
            expected_ownership_version=1,
            expected_commercial_state_revision=0,
            sellable_item_id=seeded.sellable_item_id,
            quantity=quantity,
        )
        await session.commit()
        return result


async def _reply(
    factory,
    seeded: SeededJourney,
    *,
    content: str,
    expected_ownership_version: int = 1,
):
    now = datetime.now(timezone.utc)
    inbound_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Message(
                message_id=inbound_id,
                conversation_id=seeded.conversation_id,
                timestamp=now,
                direction="inbound",
                content=content,
                content_type="text",
                language="french",
                whatsapp_message_id=f"ai6b-{inbound_id}",
                created_at=now,
            )
        )
        await session.flush()
        result = await handle_order_draft_reply(
            session,
            conversation_id=seeded.conversation_id,
            source_message_id=inbound_id,
            expected_ownership_version=expected_ownership_version,
            customer_text=content,
        )
        await session.commit()
        return result, inbound_id


async def _assert_state_counts(factory, *, orders: int = 0) -> None:
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == orders
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0


@pytest.mark.asyncio
async def test_real_ai_turn_terminal_draft_uses_authoritative_offer(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)

    class DraftAdapter:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_turn(self, _request):
            self.calls += 1
            return ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="prepare-ai6b-draft",
                        capability_name="prepare_order_draft",
                        arguments={
                            "selected_sellable_item_id": str(seeded.sellable_item_id),
                            "quantity": 2,
                        },
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
            )

    async def current_authority(_context):
        return True

    async def load_state(_conversation_id):
        return None

    adapter = DraftAdapter()
    service = AITurnService(
        adapter,
        authority_checker=current_authority,
        durable_session_factory=factory,
        commercial_state_loader=load_state,
    )
    finalized = await service.generate_finalized(
        AITurn(
            user_content="Je le prends.",
            language="french",
            expected_ownership_version=1,
            conversation_id=seeded.conversation_id,
            source_message_id=seeded.source_message_id,
            allowed_capabilities=("prepare_order_draft",),
        )
    )

    assert adapter.calls == 1
    assert finalized.audit_persisted is True
    assert finalized.audit_record.outcome.value == "order_draft_presented"
    assert finalized.outbound_message_id is not None
    assert finalized.text is not None and "308 000 CDF" in finalized.text
    async with factory() as session:
        draft = await session.scalar(select(OrderDraft))
        assert draft is not None
        assert draft.price_id == seeded.price_id
        assert draft.total_cdf == Decimal("308000.00")
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_customer_confirmation_creates_one_authoritative_pending_order_without_payment(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)

    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        assert draft is not None
        assert draft.sellable_item_id == seeded.sellable_item_id
        assert draft.price_id == seeded.price_id
        assert draft.quantity == 2
        assert draft.unit_price_usd == Decimal("55.00")
        assert draft.unit_price_cdf == Decimal("154000.00")
        assert draft.total_cdf == Decimal("308000.00")
        assert draft.status == "awaiting_confirmation"
        assert draft.confirmation_code in prepared.confirmation_text
    await _assert_state_counts(factory)

    result, _ = await _reply(
        factory,
        seeded,
        content=f"OUI {draft.confirmation_code}",
    )
    assert result is not None and result.state == "confirmed"
    assert result.order_id is not None
    async with factory() as session:
        stored = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        order = await session.get(Order, result.order_id)
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert stored is not None and stored.status == "confirmed"
        assert stored.order_id == result.order_id
        assert order is not None
        assert order.lead_id == seeded.lead_id
        assert order.customer_id == "+243810006001"
        assert order.status == "pending"
        assert order.total_amount == Decimal("308000.00")
        assert order.currency == "CDF"
        assert order.payment_type == "mobile_money"
        assert order.delivery_zone == "Kinshasa"
        assert order.delivery_method == "moto_taxi"
        assert order.hub_crm_synced is False
        assert order.hub_crm_order_id is None
        assert order.items == [
            {
                "product_id": str(stored.product_id),
                "sellable_item_id": str(stored.sellable_item_id),
                "product_name": "MBB Test Air Fryer",
                "model_label": "6L",
                "quantity": 2,
                "price_id": str(stored.price_id),
                "unit_price_usd": "55.00",
                "exchange_rate_id": str(stored.exchange_rate_id),
                "usd_to_cdf_rate": "2800.000000",
                "unit_price_cdf": "154000.00",
            }
        ]
        assert inventory is not None and inventory.status == "available"
    await _assert_state_counts(factory, orders=1)


@pytest.mark.asyncio
async def test_cancel_and_duplicate_cancel_are_idempotent(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded, quantity=1)
    async with factory() as session:
        code = await session.scalar(
            select(OrderDraft.confirmation_code).where(
                OrderDraft.draft_id == prepared.draft_id
            )
        )
    first, _ = await _reply(factory, seeded, content=f"NON {code}")
    second, _ = await _reply(factory, seeded, content=f"NON {code}")
    assert first is not None and first.state == "cancelled"
    assert second is not None and second.state == "already_cancelled"
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrderDraft)
                .where(OrderDraft.draft_id == prepared.draft_id)
            )
            == 1
        )
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_new_draft_supersedes_the_only_active_draft(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    first = await _prepare(factory, seeded, quantity=1)
    now = datetime.now(timezone.utc)
    replacement_source = uuid.uuid4()
    async with factory() as session:
        session.add(
            Message(
                message_id=replacement_source,
                conversation_id=seeded.conversation_id,
                timestamp=now,
                direction="inbound",
                content="J'en veux deux.",
                content_type="text",
                language="french",
                whatsapp_message_id=f"ai6b-{replacement_source}",
                created_at=now,
            )
        )
        await session.flush()
        second = await prepare_order_draft(
            session,
            conversation_id=seeded.conversation_id,
            source_message_id=replacement_source,
            turn_id=uuid.uuid4(),
            expected_ownership_version=1,
            expected_commercial_state_revision=0,
            sellable_item_id=seeded.sellable_item_id,
            quantity=2,
        )
        await session.commit()
    assert second.draft_id != first.draft_id
    async with factory() as session:
        statuses = list(
            (
                await session.execute(
                    select(OrderDraft.status, OrderDraft.quantity).order_by(
                        OrderDraft.created_at
                    )
                )
            ).all()
        )
        assert statuses == [("invalidated", 1), ("awaiting_confirmation", 2)]
        assert (
            await session.scalar(
                select(func.count())
                .select_from(OrderDraft)
                .where(OrderDraft.status == "awaiting_confirmation")
            )
            == 1
        )
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_duplicate_confirmation_is_safe(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        code = await session.scalar(
            select(OrderDraft.confirmation_code).where(
                OrderDraft.draft_id == prepared.draft_id
            )
        )
    first, _ = await _reply(factory, seeded, content=f"OUI {code}")
    second, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert first is not None and first.state == "confirmed"
    assert second is not None and second.state == "already_confirmed"
    assert first.order_id is not None and second.order_id == first.order_id
    await _assert_state_counts(factory, orders=1)


@pytest.mark.asyncio
async def test_concurrent_confirmation_replay_creates_exactly_one_order(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    inbound_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async with factory() as session:
        code = await session.scalar(
            select(OrderDraft.confirmation_code).where(
                OrderDraft.draft_id == prepared.draft_id
            )
        )
        session.add(
            Message(
                message_id=inbound_id,
                conversation_id=seeded.conversation_id,
                timestamp=now,
                direction="inbound",
                content=f"OUI {code}",
                content_type="text",
                language="french",
                whatsapp_message_id=f"ai6c-{inbound_id}",
                created_at=now,
            )
        )
        await session.commit()

    async def confirm_once():
        async with factory() as session:
            result = await handle_order_draft_reply(
                session,
                conversation_id=seeded.conversation_id,
                source_message_id=inbound_id,
                expected_ownership_version=1,
                customer_text=f"OUI {code}",
            )
            await session.commit()
            return result

    first, second = await asyncio.gather(confirm_once(), confirm_once())
    assert first is not None and second is not None
    assert {first.state, second.state} == {"confirmed", "already_confirmed"}
    assert first.order_id is not None and second.order_id == first.order_id
    await _assert_state_counts(factory, orders=1)


@pytest.mark.asyncio
async def test_stale_ownership_blocks_confirmation(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        conversation = await session.get(Conversation, seeded.conversation_id)
        assert draft is not None and conversation is not None
        code = draft.confirmation_code
        conversation.ownership_version = 2
        await session.commit()
    with pytest.raises(StaleOrderDraftAuthority):
        await _reply(factory, seeded, content=f"OUI {code}")
    async with factory() as session:
        status = await session.scalar(
            select(OrderDraft.status).where(OrderDraft.draft_id == prepared.draft_id)
        )
        assert status == "awaiting_confirmation"
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_stale_commercial_state_invalidates_confirmation(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        code = await session.scalar(
            select(OrderDraft.confirmation_code).where(
                OrderDraft.draft_id == prepared.draft_id
            )
        )
        await update_commercial_state(
            session,
            conversation_id=seeded.conversation_id,
            expected_revision=0,
            state_update=CommercialStateUpdate(current_goal="changed journey"),
        )
        await session.commit()
    result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert result is not None and result.state == "invalidated"
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_changed_price_creates_a_new_version_requiring_reconfirmation(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        old_price = await session.get(SellableItemPrice, seeded.price_id)
        assert draft is not None and old_price is not None
        code = draft.confirmation_code
        old_price.ended_at = datetime.now(timezone.utc)
        await session.flush()
        session.add(
            SellableItemPrice(
                price_id=uuid.uuid4(),
                sellable_item_id=seeded.sellable_item_id,
                amount=Decimal("60.00"),
                currency="USD",
                effective_at=datetime.now(timezone.utc) + timedelta(microseconds=1),
                ended_at=None,
            )
        )
        await session.commit()

    result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert result is not None and result.state == "refreshed"
    assert result.draft_version == 2
    async with factory() as session:
        versions = list(
            (
                await session.scalars(
                    select(OrderDraft)
                    .where(OrderDraft.draft_id == prepared.draft_id)
                    .order_by(OrderDraft.draft_version)
                )
            ).all()
        )
        assert [item.status for item in versions] == [
            "invalidated",
            "awaiting_confirmation",
        ]
        assert versions[1].unit_price_usd == Decimal("60.00")
        assert versions[1].total_cdf == Decimal("336000.00")
        assert versions[1].confirmation_code != code
        new_code = versions[1].confirmation_code
    old_code_result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert old_code_result is not None and old_code_result.state == "invalidated"
    async with factory() as session:
        active = await session.scalar(
            select(OrderDraft).where(
                OrderDraft.draft_id == prepared.draft_id,
                OrderDraft.draft_version == 2,
            )
        )
        assert active is not None
        assert active.status == "awaiting_confirmation"
        assert active.confirmation_code == new_code
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_changed_exchange_rate_requires_reconfirmation_before_order(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        current_rate = await session.scalar(
            select(ExchangeRate).where(ExchangeRate.ended_at.is_(None))
        )
        assert draft is not None and current_rate is not None
        code = draft.confirmation_code
        changed_at = datetime.now(timezone.utc)
        current_rate.ended_at = changed_at
        session.add(
            ExchangeRate(
                exchange_rate_id=uuid.uuid4(),
                base_currency="USD",
                quote_currency="CDF",
                rate=Decimal("3000.000000"),
                effective_at=changed_at + timedelta(microseconds=1),
                ended_at=None,
            )
        )
        await session.commit()

    result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert result is not None and result.state == "refreshed"
    async with factory() as session:
        replacement = await session.scalar(
            select(OrderDraft).where(
                OrderDraft.draft_id == prepared.draft_id,
                OrderDraft.draft_version == 2,
            )
        )
        assert replacement is not None
        assert replacement.unit_price_cdf == Decimal("165000.00")
        assert replacement.total_cdf == Decimal("330000.00")
        assert replacement.order_id is None
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_missing_lead_authority_invalidates_without_order_or_payment(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        lead = await session.get(Lead, seeded.lead_id)
        assert draft is not None and lead is not None
        code = draft.confirmation_code
        await session.execute(delete(Lead).where(Lead.lead_id == seeded.lead_id))
        await session.commit()

    result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert result is not None and result.state == "invalidated"
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        assert draft is not None
        assert draft.status == "invalidated"
        assert draft.resolution_code == "order_authority_unavailable"
        assert draft.order_id is None
    await _assert_state_counts(factory)


@pytest.mark.asyncio
async def test_unavailable_offer_blocks_confirmation(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert draft is not None and inventory is not None
        code = draft.confirmation_code
        inventory.status = "out_of_stock"
        inventory.updated_at = datetime.now(timezone.utc)
        await session.commit()
    result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert result is not None and result.state == "invalidated"
    await _assert_state_counts(factory)
