from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

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
                # Match M1's database-assigned receipt time. Mixing the host
                # clock with PostgreSQL NOW() can reorder adjacent messages.
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


def _block_m1_external_effects(
    monkeypatch: pytest.MonkeyPatch,
    factory,
    *,
    whatsapp_send_enabled: bool,
) -> list[str]:
    import app.adapters as adapters
    import app.ai.turn as ai_turn
    import app.database as database
    from app.adapters.fx.exchange_rate_api import ExchangeRateAPIAdapter
    from app.tasks import m1

    external_calls: list[str] = []

    def blocked(name: str):
        def record(*_args, **_kwargs):
            external_calls.append(name)
            pytest.fail(f"unexpected external action: {name}")

        return record

    async def blocked_fx(*_args, **_kwargs):
        external_calls.append("exchange_rate_provider")
        pytest.fail("unexpected external action: exchange_rate_provider")

    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(ai_turn, "get_ai_turn_service", blocked("provider_inference"))
    monkeypatch.setattr(adapters, "get_crm_adapter", blocked("crm"))
    monkeypatch.setattr(adapters, "get_inventory_adapter", blocked("inventory_adapter"))
    monkeypatch.setattr(adapters, "get_payment_adapter", blocked("payment_provider"))
    monkeypatch.setattr(adapters, "get_messaging_adapter", blocked("whatsapp"))
    monkeypatch.setattr(ExchangeRateAPIAdapter, "fetch_usd_cdf", blocked_fx)
    monkeypatch.setattr(m1.celery_app, "send_task", blocked("celery_external_effect"))
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(
            whatsapp_send_enabled=whatsapp_send_enabled,
            m1_maps_fanout_enabled=False,
        ),
    )
    return external_calls


async def _process_m1_confirmation(
    *,
    content: str,
    sequence: int,
) -> dict:
    from app.tasks import m1

    class TaskStub:
        request = SimpleNamespace(retries=0)

        def retry(self, **_kwargs):
            raise AssertionError("M1 unexpectedly requested a Celery retry")

    return await m1._process(
        task=TaskStub(),
        message_id=str(uuid.uuid4()),
        customer_phone="+243810006001",
        content=content,
        content_type="text",
        timestamp=datetime.now(timezone.utc).isoformat(),
        whatsapp_message_id=f"ai6d-confirmation-{sequence}-{uuid.uuid4()}",
    )


@pytest.mark.asyncio
async def test_real_ai_turn_terminal_draft_uses_authoritative_offer(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)

    from app.modules.product_offer.service import get_product_offer

    external_calls = _block_m1_external_effects(
        monkeypatch, factory, whatsapp_send_enabled=False,
    )
    async with factory() as session:
        lead = await session.get(Lead, seeded.lead_id)
        assert lead is not None and lead.qualified_at is not None
        assert lead.stage == "decision" and lead.score == "hot"
        offer = await get_product_offer(session, seeded.sellable_item_id)
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert offer is not None and inventory is not None
        assert offer.product_name == "MBB Test Air Fryer"
        assert offer.sellable_item_id == seeded.sellable_item_id
        assert offer.price_id == seeded.price_id
        assert offer.current_usd_price == Decimal("55.00")
        assert offer.inventory_status == "available" and offer.is_sellable_now
        assert offer.offer_status == "sellable_now"
        quote = offer.derived_cdf_quote
        assert quote is not None
        assert quote.usd_to_cdf_rate == Decimal("2800")
        assert quote.cdf_amount == Decimal("154000.00")
        inventory_before = (inventory.status, inventory.updated_at)

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
        assert draft.product_id == offer.product_id
        assert draft.sellable_item_id == offer.sellable_item_id
        assert draft.unit_price_usd == offer.current_usd_price
        assert draft.exchange_rate_id == quote.exchange_rate_id
        assert draft.unit_price_cdf == quote.cdf_amount
        assert draft.inventory_status == offer.inventory_status
        assert draft.inventory_updated_at == offer.inventory_updated_at
        assert draft.quantity == 2 and draft.status == "awaiting_confirmation"
        confirmation = f"OUI {draft.confirmation_code}"
    await _assert_state_counts(factory)

    # An unqualified yes is not the exact application-owned confirmation.
    ambiguous, _ = await _reply(factory, seeded, content="OUI")
    assert ambiguous is None
    await _assert_state_counts(factory)
    first = await _process_m1_confirmation(content=confirmation, sequence=1)
    replay = await _process_m1_confirmation(content=confirmation, sequence=2)
    assert first["status"] == "order_draft_confirmed"
    assert replay["status"] == "order_draft_already_confirmed"
    assert replay["order_id"] == first["order_id"]
    assert first["send_status"] == replay["send_status"] == "skipped"
    await _assert_state_counts(factory, orders=1)

    async with factory() as session:
        stored = await session.get(Order, uuid.UUID(first["order_id"]))
        confirmed = await session.scalar(select(OrderDraft))
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert stored is not None and confirmed is not None and inventory is not None
        assert confirmed.status == "confirmed" and confirmed.order_id == stored.order_id
        assert stored.status == "pending" and stored.total_amount == draft.total_cdf
        assert stored.confirmed_at is None and stored.delivered_at is None
        assert stored.hub_crm_synced is False and stored.hub_crm_order_id is None
        assert (inventory.status, inventory.updated_at) == inventory_before
        item = stored.items[0]
        assert item["product_id"] == str(offer.product_id)
        assert item["sellable_item_id"] == str(offer.sellable_item_id)
        assert item["product_name"] == offer.product_name
        assert item["quantity"] == 2
        assert item["price_id"] == str(offer.price_id)
        assert Decimal(item["unit_price_usd"]) == offer.current_usd_price
        assert item["exchange_rate_id"] == str(quote.exchange_rate_id)
        assert Decimal(item["usd_to_cdf_rate"]) == quote.usd_to_cdf_rate
        assert Decimal(item["unit_price_cdf"]) == quote.cdf_amount

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api.deps import get_current_role, get_db
    from app.api.v1.orders import router

    api = FastAPI()
    api.include_router(router, prefix="/api/v1")

    async def db():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_db] = db
    api.dependency_overrides[get_current_role] = lambda: "admin"
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.get(f"/api/v1/orders/{first['order_id']}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "pending" and body["payment_method"] is None
    assert Decimal(body["total_cdf"]) == Decimal("308000.00")
    assert body["items"] == [{
        "product_id": str(offer.product_id), "quantity": 2,
        "unit_price_cdf": "154000.00",
    }]
    assert "payment_status" not in body and "delivery_method" not in body
    assert external_calls == []


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
        from app.api.v1.orders import get_order

        response = await get_order(order.order_id, session)
        assert response.status.value == "pending"
        assert response.payment_method is None
        assert response.total_cdf == Decimal("308000.00")
        assert order.confirmed_at is None and order.delivered_at is None
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
async def test_retired_creation_and_historical_read_status_api(engine: AsyncEngine):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api.deps import get_current_role
    from app.api.v1.orders import router
    from app.database import get_db

    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    api = FastAPI()
    api.include_router(router, prefix="/api/v1")

    async def db():
        async with factory() as session:
            yield session

    api.dependency_overrides[get_db] = db
    api.dependency_overrides[get_current_role] = lambda: "admin"
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        for price in ("0.01", "154000.00"):
            response = await client.post("/api/v1/orders", json={
                "lead_id": str(seeded.lead_id),
                "items": [{"product_id": "caller-product", "quantity": 1, "unit_price_cdf": price}],
                "delivery_zone": "Gombe", "payment_method": "orange_money",
            }, headers={"X-Idempotency-Key": "obsolete-key"})
            assert response.status_code == 404
        await _assert_state_counts(factory)

        for method in (None, "orange_money", "airtel_money", "mpesa", "cash", "bank_transfer"):
            order_id = uuid.uuid4()
            async with factory() as session:
                session.add(Order(
                    order_id=order_id, lead_id=seeded.lead_id, customer_id="+243810006001",
                    items=[{"product_id": "historical-sku", "quantity": 1, "unit_price_cdf": 1000}],
                    total_amount=Decimal("1000.00"), currency="CDF", status="pending",
                    payment_type={"cash": "cod", "bank_transfer": "bank_transfer"}.get(method, "mobile_money"),
                    delivery_zone="Gombe", delivery_method="moto_taxi",
                ))
                await session.flush()
                if method is not None:
                    session.add(Payment(order_id=order_id, method=method, amount=Decimal("1000.00"), status="pending"))
                await session.commit()
            url = f"/api/v1/orders/{order_id}"
            response = await client.get(url)
            assert response.status_code == 200
            body = response.json()
            assert body["payment_method"] == method
            assert body["status"] == "pending"
            assert body["items"][0]["product_id"] == "historical-sku"
            assert "delivery_method" not in body and "payment_status" not in body
            assert (await client.put(url + "/status", json={"status": "delivered"})).status_code == 409
            assert (await client.put(url + "/status", json={"status": "cancelled"})).status_code == 200
            assert (await client.get(url)).json()["status"] == "cancelled"
        assert (await client.get(f"/api/v1/orders/{uuid.uuid4()}")).status_code == 404


@pytest.mark.asyncio
async def test_m1_exact_confirmation_creates_and_replays_one_pending_order(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert draft is not None and inventory is not None
        confirmation = f"OUI {draft.confirmation_code}"
        inventory_before = (inventory.status, inventory.updated_at)

    external_calls = _block_m1_external_effects(
        monkeypatch,
        factory,
        whatsapp_send_enabled=True,
    )

    first = await _process_m1_confirmation(content=confirmation, sequence=1)
    replay = await _process_m1_confirmation(content=confirmation, sequence=2)

    assert first["status"] == "order_draft_confirmed"
    assert first["send_status"] == "skipped"
    assert replay["status"] == "order_draft_already_confirmed"
    assert replay["send_status"] == "skipped"
    assert replay["order_id"] == first["order_id"]
    assert external_calls == []

    async with factory() as session:
        stored = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        orders = list((await session.scalars(select(Order))).all())
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert stored is not None and stored.status == "confirmed"
        assert stored.order_id == uuid.UUID(first["order_id"])
        assert len(orders) == 1
        assert orders[0].order_id == stored.order_id
        assert orders[0].status == "pending"
        assert orders[0].hub_crm_synced is False
        assert orders[0].hub_crm_order_id is None
        assert inventory is not None
        assert (inventory.status, inventory.updated_at) == inventory_before
    await _assert_state_counts(factory, orders=1)


@pytest.mark.asyncio
async def test_m1_changed_terms_require_reconfirmation_without_order(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        old_price = await session.get(SellableItemPrice, seeded.price_id)
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert draft is not None and old_price is not None and inventory is not None
        confirmation = f"OUI {draft.confirmation_code}"
        inventory_before = (inventory.status, inventory.updated_at)
        changed_at = datetime.now(timezone.utc)
        old_price.ended_at = changed_at
        session.add(
            SellableItemPrice(
                price_id=uuid.uuid4(),
                sellable_item_id=seeded.sellable_item_id,
                amount=Decimal("60.00"),
                currency="USD",
                effective_at=changed_at + timedelta(microseconds=1),
                ended_at=None,
            )
        )
        await session.commit()

    external_calls = _block_m1_external_effects(
        monkeypatch,
        factory,
        whatsapp_send_enabled=False,
    )

    result = await _process_m1_confirmation(content=confirmation, sequence=1)

    assert result["status"] == "order_draft_refreshed"
    assert result["draft_version"] == 2
    assert result["send_status"] == "skipped"
    assert "order_id" not in result
    assert external_calls == []
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
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert [item.status for item in versions] == [
            "invalidated",
            "awaiting_confirmation",
        ]
        assert versions[1].unit_price_usd == Decimal("60.00")
        assert versions[1].confirmation_code not in confirmation
        assert inventory is not None
        assert (inventory.status, inventory.updated_at) == inventory_before
    await _assert_state_counts(factory)


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
