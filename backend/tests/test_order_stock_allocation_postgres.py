"""Disposable PostgreSQL evidence for Order-linked full Stock allocation."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.inventory import InventoryRecord
from app.models.lead import Lead
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.inventory.service import (
    InventoryAllocationConflict,
    InventoryInsufficientStock,
    adjust_stock,
    receive_stock,
)
from app.modules.m7_conversion.order_allocation import (
    OrderStockAllocationPaymentRequired,
    OrderStockAllocationStateConflict,
    allocate_paid_order_stock,
)

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="Disposable PostgreSQL required"
)

TRUNCATE = text(
    "TRUNCATE TABLE mbb.customers, mbb.products, mbb.operator_accounts "
    "RESTART IDENTITY CASCADE"
)


@dataclass(frozen=True)
class AllocationFixture:
    factory: async_sessionmaker
    lead_id: uuid.UUID
    customer_id: str
    sellable_item_id: uuid.UUID
    inventory_id: uuid.UUID
    administrator: CommerceAdminContext


@pytest_asyncio.fixture(loop_scope="function")
async def allocation_fixture() -> AllocationFixture:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(TRUNCATE)

    now = datetime.now(timezone.utc)
    customer = Customer(
        phone_number="+243810009001",
        name="Allocation Fixture",
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
    )
    lead = Lead(
        lead_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        conversation_id=conversation.conversation_id,
        score="hot",
        score_value=9,
        stage="decision",
        intent="product_inquiry",
        product_interest=["allocation-fixture"],
        source="test",
        relance_count=0,
    )
    product = Product(
        product_id=uuid.uuid4(),
        name="Allocation Fixture Product",
        category_code="test",
        description="Synthetic allocation evidence",
        active=True,
    )
    item = SellableItem(
        sellable_item_id=uuid.uuid4(),
        product_id=product.product_id,
        model_label="Single line",
        sku="ALLOC-ONE",
        attributes={},
        active=True,
    )
    inventory = InventoryRecord(
        inventory_id=uuid.uuid4(),
        sellable_item_id=item.sellable_item_id,
        status="available",
        quantity=5,
        updated_at=now,
    )
    administrator = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="allocation.admin",
        display_name="Allocation Administrator",
        email_normalized=None,
        password_hash="unused-test-hash",
        role="administrator",
        status="active",
        auth_version=1,
        must_change_password=False,
        password_changed_at=now,
    )
    async with factory() as session:
        session.add_all(
            [customer, conversation, lead, product, item, inventory, administrator]
        )
        await session.commit()

    value = AllocationFixture(
        factory=factory,
        lead_id=lead.lead_id,
        customer_id=customer.phone_number,
        sellable_item_id=item.sellable_item_id,
        inventory_id=inventory.inventory_id,
        administrator=CommerceAdminContext(
            actor_account_id=administrator.account_id,
            request_id="allocation-test",
        ),
    )
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await engine.dispose()


async def _order(
    fixture: AllocationFixture,
    *,
    quantity: int,
    status: str = "confirmed",
    payment_status: str = "completed",
) -> uuid.UUID:
    order_id = uuid.uuid4()
    total = Decimal(quantity * 1000)
    completed_at = datetime.now(timezone.utc) if payment_status == "completed" else None
    async with fixture.factory() as session:
        session.add(
            Order(
                order_id=order_id,
                lead_id=fixture.lead_id,
                customer_id=fixture.customer_id,
                items=[
                    {
                        "sellable_item_id": str(fixture.sellable_item_id),
                        "quantity": quantity,
                        "unit_price_cdf": "1000.00",
                    }
                ],
                total_amount=total,
                currency="CDF",
                payment_type="bank_transfer",
                delivery_zone="Kinshasa",
                delivery_method="moto_taxi",
                status=status,
            )
        )
        await session.flush()
        # This is isolated durable prerequisite data, not a simulated provider
        # callback or a claim that provider settlement exists.
        session.add(
            Payment(
                payment_id=uuid.uuid4(),
                order_id=order_id,
                method="bank_transfer",
                amount=total,
                currency="CDF",
                status=payment_status,
                completed_at=completed_at,
            )
        )
        await session.commit()
    return order_id


async def _allocate(fixture: AllocationFixture, order_id: uuid.UUID):
    async with fixture.factory() as session:
        try:
            result = await allocate_paid_order_stock(session, order_id=order_id)
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise


@pytest.mark.asyncio
async def test_competing_paid_orders_cannot_oversubscribe(
    allocation_fixture: AllocationFixture,
) -> None:
    first_id = await _order(allocation_fixture, quantity=3)
    second_id = await _order(allocation_fixture, quantity=3)

    results = await asyncio.gather(
        _allocate(allocation_fixture, first_id),
        _allocate(allocation_fixture, second_id),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    failure = next(result for result in results if isinstance(result, Exception))
    assert isinstance(failure, InventoryInsufficientStock)
    assert failure.available == 2 and failure.requested == 3
    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert inventory.quantity == 5
        assert inventory.allocated_quantity == 3
        assert inventory.status == "available"
        assert await session.scalar(
            select(func.count()).select_from(StockAllocation)
        ) == 1


@pytest.mark.asyncio
async def test_replay_survives_receiving_and_adjustment_without_double_decrement(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id = await _order(allocation_fixture, quantity=2)
    first = await _allocate(allocation_fixture, order_id)
    replay = await _allocate(allocation_fixture, order_id)
    assert first.replayed is False and replay.replayed is True
    assert replay.allocation.order_id == first.allocation.order_id

    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        with pytest.raises(InventoryAllocationConflict):
            await adjust_stock(
                session,
                sellable_item_id=allocation_fixture.sellable_item_id,
                corrected_quantity=1,
                reason="Invalid count below committed allocation",
                expected_quantity=5,
                expected_updated_at=inventory.updated_at,
                operation_key=uuid.uuid4(),
                administrator=allocation_fixture.administrator,
            )
        await session.rollback()

    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert inventory.quantity == 5 and inventory.allocated_quantity == 2
        await receive_stock(
            session,
            sellable_item_id=allocation_fixture.sellable_item_id,
            received_amount=4,
            expected_quantity=5,
            expected_updated_at=inventory.updated_at,
            operation_key=uuid.uuid4(),
            administrator=allocation_fixture.administrator,
        )
        await session.commit()

    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        await adjust_stock(
            session,
            sellable_item_id=allocation_fixture.sellable_item_id,
            corrected_quantity=5,
            reason="Verified available count after allocation",
            expected_quantity=9,
            expected_updated_at=inventory.updated_at,
            operation_key=uuid.uuid4(),
            administrator=allocation_fixture.administrator,
        )
        await session.commit()

    late_replay = await _allocate(allocation_fixture, order_id)
    assert late_replay.replayed is True
    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        allocation = await session.get(StockAllocation, order_id)
        assert inventory.quantity == 5 and inventory.allocated_quantity == 2
        assert (
            allocation.before_available_quantity,
            allocation.quantity,
            allocation.after_available_quantity,
        ) == (5, 2, 3)
        with pytest.raises(DBAPIError, match="stock allocations are append-only"):
            await session.execute(
                update(StockAllocation)
                .where(StockAllocation.order_id == order_id)
                .values(quantity=1)
            )


@pytest.mark.asyncio
async def test_adjustment_lock_serializes_allocation_and_rollback_is_atomic(
    allocation_fixture: AllocationFixture,
) -> None:
    blocked_order_id = await _order(allocation_fixture, quantity=4)
    async with allocation_fixture.factory() as adjustment_session:
        inventory = await adjustment_session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        await adjust_stock(
            adjustment_session,
            sellable_item_id=allocation_fixture.sellable_item_id,
            corrected_quantity=3,
            reason="Verified lower available count",
            expected_quantity=5,
            expected_updated_at=inventory.updated_at,
            operation_key=uuid.uuid4(),
            administrator=allocation_fixture.administrator,
        )
        allocation_task = asyncio.create_task(
            _allocate(allocation_fixture, blocked_order_id)
        )
        await asyncio.sleep(0.1)
        assert not allocation_task.done(), "allocation bypassed the Inventory lock"
        await adjustment_session.commit()
    with pytest.raises(InventoryInsufficientStock):
        await asyncio.wait_for(allocation_task, timeout=5)

    rollback_order_id = await _order(allocation_fixture, quantity=2)
    async with allocation_fixture.factory() as session:
        await allocate_paid_order_stock(session, order_id=rollback_order_id)
        await session.rollback()
    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert inventory.quantity == 3 and inventory.allocated_quantity == 0
        assert await session.get(StockAllocation, rollback_order_id) is None


@pytest.mark.asyncio
async def test_allocation_requires_confirmed_order_and_matching_completed_payment(
    allocation_fixture: AllocationFixture,
) -> None:
    pending_order_id = await _order(
        allocation_fixture, quantity=1, status="pending"
    )
    unpaid_order_id = await _order(
        allocation_fixture, quantity=1, payment_status="pending"
    )
    async with allocation_fixture.factory() as session:
        with pytest.raises(OrderStockAllocationStateConflict):
            await allocate_paid_order_stock(session, order_id=pending_order_id)
        await session.rollback()
    async with allocation_fixture.factory() as session:
        with pytest.raises(OrderStockAllocationPaymentRequired):
            await allocate_paid_order_stock(session, order_id=unpaid_order_id)
        await session.rollback()
    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert inventory.quantity == 5
        assert await session.scalar(
            select(func.count()).select_from(StockAllocation)
        ) == 0
