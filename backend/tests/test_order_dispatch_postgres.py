"""Disposable PostgreSQL evidence for guarded physical Order dispatch."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError

from app.models.inventory import InventoryRecord
from app.models.operator_audit import OperatorAuditEvent
from app.models.order import Order
from app.models.payment import Payment
from app.models.stock_movement import StockMovement
from app.modules.inventory.service import adjust_stock
from app.modules.m7_conversion import order_dispatch
from app.modules.m7_conversion.order_dispatch import (
    OrderDispatchForbidden,
    OrderDispatchIdempotencyConflict,
    OrderDispatchMethodUnsupported,
    OrderDispatchPaymentRequired,
    OrderDispatchResult,
    OrderDispatchStale,
    OrderDispatchStateConflict,
    OrderDispatchUnavailable,
    dispatch_prepared_order,
)
from app.modules.m7_conversion.order_fulfillment import prepare_confirmed_order
from app.modules.m7_conversion.service import (
    DeliveryConfirmationUnavailable,
    DispatchPrerequisiteUnavailable,
    update_order_status,
)
from test_order_stock_allocation_postgres import (
    AllocationFixture,
    _allocate,
    _order,
)

pytest_plugins = ("test_order_stock_allocation_postgres",)
pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"),
    reason="Disposable PostgreSQL required",
)


async def _prepared_order(fixture: AllocationFixture, *, quantity: int = 2):
    order_id = await _order(fixture, quantity=quantity)
    await _allocate(fixture, order_id)
    async with fixture.factory() as session:
        order = await session.get(Order, order_id)
        result = await prepare_confirmed_order(
            session,
            order_id=order_id,
            expected_updated_at=order.updated_at,
            idempotency_key=uuid.uuid4(),
            actor_kind="service",
            actor_display_name="dispatch-fixture",
        )
        return order_id, result.order.updated_at


async def _dispatch(
    fixture: AllocationFixture,
    order_id: uuid.UUID,
    expected_updated_at,
    key: uuid.UUID,
    *,
    reference: str = "MBB-HANDOFF-1001",
) -> OrderDispatchResult:
    async with fixture.factory() as session:
        return await dispatch_prepared_order(
            session,
            order_id=order_id,
            expected_updated_at=expected_updated_at,
            idempotency_key=key,
            handoff_reference=reference,
            attested=True,
            actor_account_id=fixture.administrator.actor_account_id,
            actor_display_name="Allocation Administrator",
            actor_role="administrator",
            request_id="dispatch-test-request",
        )


@pytest.mark.asyncio
async def test_dispatch_consumes_allocation_once_with_movement_and_audit(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, reviewed_at = await _prepared_order(allocation_fixture)
    key = uuid.uuid4()
    first = await _dispatch(allocation_fixture, order_id, reviewed_at, key)
    replay = await _dispatch(allocation_fixture, order_id, reviewed_at, key)

    assert first.replayed is False and replay.replayed is True
    assert replay.movement.movement_id == first.movement.movement_id
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, order_id)
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        movement = await session.scalar(
            select(StockMovement).where(StockMovement.order_id == order_id)
        )
        audit = await session.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.action == "order.dispatched",
                OperatorAuditEvent.target_id == str(order_id),
            )
        )
        assert order.status == "delivering" and order.delivered_at is None
        assert order.updated_at > reviewed_at
        assert inventory.quantity == 3 and inventory.allocated_quantity == 0
        assert inventory.status == "available"
        assert movement.operation_kind == "dispatch"
        assert movement.before_quantity == 5 and movement.after_quantity == 3
        assert movement.dispatched_quantity == 2
        assert movement.handoff_reference == "MBB-HANDOFF-1001"
        assert audit.event_metadata["movement_id"] == str(movement.movement_id)
        assert audit.event_metadata["allocated_before_quantity"] == 2
        assert audit.event_metadata["allocated_after_quantity"] == 0
        assert await session.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.dispatched")
        ) == 1
        with pytest.raises(DBAPIError, match="stock movements are append-only"):
            await session.execute(
                update(StockMovement)
                .where(StockMovement.movement_id == movement.movement_id)
                .values(handoff_reference="tampered")
            )


@pytest.mark.asyncio
async def test_dispatch_replay_conflict_and_competing_requests_deduct_once(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, reviewed_at = await _prepared_order(allocation_fixture)
    key = uuid.uuid4()
    await _dispatch(allocation_fixture, order_id, reviewed_at, key)
    with pytest.raises(OrderDispatchIdempotencyConflict):
        await _dispatch(
            allocation_fixture,
            order_id,
            reviewed_at,
            key,
            reference="MBB-HANDOFF-CHANGED",
        )

    competing_id, competing_at = await _prepared_order(allocation_fixture, quantity=1)
    results = await asyncio.gather(
        _dispatch(allocation_fixture, competing_id, competing_at, uuid.uuid4()),
        _dispatch(allocation_fixture, competing_id, competing_at, uuid.uuid4()),
        return_exceptions=True,
    )
    assert sum(isinstance(value, OrderDispatchResult) for value in results) == 1
    failure = next(value for value in results if isinstance(value, Exception))
    assert isinstance(failure, OrderDispatchStateConflict)
    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert inventory.quantity == 2 and inventory.allocated_quantity == 0
        assert await session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.operation_kind == "dispatch")
        ) == 2


@pytest.mark.asyncio
async def test_dispatch_rejects_stale_unauthorized_unsupported_and_unpaid(
    allocation_fixture: AllocationFixture,
) -> None:
    stale_id, reviewed_at = await _prepared_order(allocation_fixture)
    with pytest.raises(OrderDispatchStale):
        await _dispatch(
            allocation_fixture,
            stale_id,
            reviewed_at - timedelta(microseconds=1),
            uuid.uuid4(),
        )
    async with allocation_fixture.factory() as session:
        with pytest.raises(OrderDispatchForbidden):
            await dispatch_prepared_order(
                session,
                order_id=stale_id,
                expected_updated_at=reviewed_at,
                idempotency_key=uuid.uuid4(),
                handoff_reference="MBB-HANDOFF-UNAUTHORIZED",
                attested=True,
                actor_account_id=uuid.uuid4(),
                actor_display_name="Operator",
                actor_role="operator",
                request_id="dispatch-test-request",
            )

    unsupported_id, unsupported_at = await _prepared_order(allocation_fixture, quantity=1)
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, unsupported_id)
        order.delivery_method = "spot_pickup"
        await session.commit()
    with pytest.raises(OrderDispatchMethodUnsupported):
        await _dispatch(
            allocation_fixture, unsupported_id, unsupported_at, uuid.uuid4()
        )

    unpaid_id, unpaid_at = await _prepared_order(allocation_fixture, quantity=1)
    async with allocation_fixture.factory() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == unpaid_id)
        )
        payment.status = "refunded"
        await session.commit()
    with pytest.raises(OrderDispatchPaymentRequired):
        await _dispatch(allocation_fixture, unpaid_id, unpaid_at, uuid.uuid4())


@pytest.mark.asyncio
async def test_dispatch_serializes_stock_adjustment_and_rolls_back_atomically(
    allocation_fixture: AllocationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id, reviewed_at = await _prepared_order(allocation_fixture)
    async with allocation_fixture.factory() as adjustment_session:
        inventory = await adjustment_session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        await adjust_stock(
            adjustment_session,
            sellable_item_id=allocation_fixture.sellable_item_id,
            corrected_quantity=4,
            reason="Verified count before handoff",
            expected_quantity=5,
            expected_updated_at=inventory.updated_at,
            operation_key=uuid.uuid4(),
            administrator=allocation_fixture.administrator,
        )
        dispatch_task = asyncio.create_task(
            _dispatch(allocation_fixture, order_id, reviewed_at, uuid.uuid4())
        )
        await asyncio.sleep(0.1)
        assert not dispatch_task.done(), "dispatch bypassed the Inventory lock"
        await adjustment_session.commit()
    await asyncio.wait_for(dispatch_task, timeout=5)
    async with allocation_fixture.factory() as session:
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert inventory.quantity == 2 and inventory.allocated_quantity == 0

    rollback_id, rollback_at = await _prepared_order(allocation_fixture, quantity=1)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(order_dispatch, "append_operator_audit_event", fail_audit)
    with pytest.raises(OrderDispatchUnavailable):
        await _dispatch(allocation_fixture, rollback_id, rollback_at, uuid.uuid4())
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, rollback_id)
        inventory = await session.get(
            InventoryRecord, allocation_fixture.inventory_id
        )
        assert order.status == "preparing"
        assert inventory.quantity == 2 and inventory.allocated_quantity == 1
        assert await session.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.order_id == rollback_id)
        ) == 0


@pytest.mark.asyncio
async def test_legacy_service_cannot_bypass_dispatch_or_delivery(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, _ = await _prepared_order(allocation_fixture)
    async with allocation_fixture.factory() as session:
        with pytest.raises(DispatchPrerequisiteUnavailable):
            await update_order_status(
                session, order_id=order_id, new_status="delivering"
            )
        order = await session.get(Order, order_id)
        order.status = "delivering"
        await session.commit()
        with pytest.raises(DeliveryConfirmationUnavailable):
            await update_order_status(
                session, order_id=order_id, new_status="delivered"
            )
