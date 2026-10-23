"""Disposable PostgreSQL evidence for guarded Order preparation."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, func, select

from app.api.v1.orders import update_order_status as http_update_order_status
from app.models.operator_audit import OperatorAuditEvent
from app.models.order import Order
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.modules.m7_conversion import order_fulfillment
from app.modules.m7_conversion.order_fulfillment import (
    OrderPreparationAllocationRequired,
    OrderPreparationForbidden,
    OrderPreparationPaymentRequired,
    OrderPreparationResult,
    OrderPreparationStale,
    OrderPreparationStateConflict,
    OrderPreparationUnavailable,
    prepare_confirmed_order,
)
from app.schemas.common import OrderStatus
from app.schemas.orders import OrderStatusUpdate
from app.tasks.conversion import update_order_status as celery_update_order_status
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


async def _ready_order(fixture: AllocationFixture, *, quantity: int = 2):
    order_id = await _order(fixture, quantity=quantity)
    await _allocate(fixture, order_id)
    async with fixture.factory() as session:
        order = await session.get(Order, order_id)
        return order_id, order.updated_at


async def _prepare(
    fixture: AllocationFixture,
    order_id: uuid.UUID,
    expected_updated_at,
    idempotency_key: uuid.UUID,
) -> OrderPreparationResult:
    async with fixture.factory() as session:
        return await prepare_confirmed_order(
            session,
            order_id=order_id,
            expected_updated_at=expected_updated_at,
            idempotency_key=idempotency_key,
            actor_kind="service",
            actor_display_name="fulfillment-test",
            request_id="fulfillment-test-request",
        )


@pytest.mark.asyncio
async def test_http_prepares_fully_paid_and_allocated_order_with_audit(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, expected_updated_at = await _ready_order(allocation_fixture)
    idempotency_key = uuid.uuid4()

    async with allocation_fixture.factory() as session:
        response = await http_update_order_status(
            order_id,
            OrderStatusUpdate(
                status=OrderStatus.preparing,
                expected_updated_at=expected_updated_at,
                idempotency_key=idempotency_key,
            ),
            session,
            current_role="admin",
            request_id="fulfillment-http-request",
        )

    assert response.status == "preparing"
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, order_id)
        audit = await session.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.action == "order.prepared",
                OperatorAuditEvent.target_id == str(order_id),
            )
        )
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id)
        )
        allocation = await session.get(StockAllocation, order_id)

    assert order.status == "preparing"
    assert order.updated_at > expected_updated_at
    assert payment.status == "completed" and payment.completed_at is not None
    assert allocation.quantity == 2
    assert audit.outcome == "succeeded"
    assert audit.actor_display_name == "authenticated-order-api:admin"
    assert audit.request_id == "fulfillment-http-request"
    assert audit.event_metadata["previous_status"] == "confirmed"
    assert audit.event_metadata["new_status"] == "preparing"


@pytest.mark.asyncio
async def test_preparation_requires_payment_record(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id = await _order(allocation_fixture, quantity=1)
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, order_id)
        expected_updated_at = order.updated_at
        await session.execute(delete(Payment).where(Payment.order_id == order_id))
        await session.commit()
    async with allocation_fixture.factory() as session:
        with pytest.raises(OrderPreparationPaymentRequired):
            await prepare_confirmed_order(
                session,
                order_id=order_id,
                expected_updated_at=expected_updated_at,
                idempotency_key=uuid.uuid4(),
                actor_kind="service",
                actor_display_name="fulfillment-test",
            )


@pytest.mark.parametrize("mismatch", ["status", "amount", "currency"])
@pytest.mark.asyncio
async def test_preparation_requires_matching_verified_payment(
    allocation_fixture: AllocationFixture,
    mismatch: str,
) -> None:
    order_id = await _order(allocation_fixture, quantity=1)
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, order_id)
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id)
        )
        if mismatch == "status":
            payment.status = "pending"
            payment.completed_at = None
        elif mismatch == "amount":
            payment.amount += 1
        else:
            payment.currency = "USD"
        await session.commit()
        with pytest.raises(OrderPreparationPaymentRequired):
            await prepare_confirmed_order(
                session,
                order_id=order_id,
                expected_updated_at=order.updated_at,
                idempotency_key=uuid.uuid4(),
                actor_kind="service",
                actor_display_name="fulfillment-test",
            )


@pytest.mark.asyncio
async def test_preparation_rejects_unauthorized_actor(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, expected_updated_at = await _ready_order(allocation_fixture)
    async with allocation_fixture.factory() as session:
        with pytest.raises(OrderPreparationForbidden):
            await prepare_confirmed_order(
                session,
                order_id=order_id,
                expected_updated_at=expected_updated_at,
                idempotency_key=uuid.uuid4(),
                actor_kind="human",
                actor_display_name="unauthorized-test-operator",
                actor_role="operator",
                actor_account_id=uuid.uuid4(),
            )


@pytest.mark.asyncio
async def test_preparation_rejects_partial_allocation(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id = await _order(allocation_fixture, quantity=2)
    async with allocation_fixture.factory() as session:
        order = await session.get(Order, order_id)
        session.add(
            StockAllocation(
                order_id=order_id,
                sellable_item_id=allocation_fixture.sellable_item_id,
                quantity=1,
                before_available_quantity=5,
                after_available_quantity=4,
            )
        )
        await session.commit()
        with pytest.raises(OrderPreparationAllocationRequired):
            await prepare_confirmed_order(
                session,
                order_id=order_id,
                expected_updated_at=order.updated_at,
                idempotency_key=uuid.uuid4(),
                actor_kind="service",
                actor_display_name="fulfillment-test",
            )


@pytest.mark.asyncio
async def test_preparation_replay_and_stale_or_conflicting_writes(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, expected_updated_at = await _ready_order(allocation_fixture)
    key = uuid.uuid4()
    first = await _prepare(allocation_fixture, order_id, expected_updated_at, key)
    replay = await _prepare(allocation_fixture, order_id, expected_updated_at, key)

    assert first.replayed is False and replay.replayed is True
    assert replay.order.updated_at == first.order.updated_at
    with pytest.raises(OrderPreparationStateConflict):
        await _prepare(allocation_fixture, order_id, expected_updated_at, uuid.uuid4())

    stale_id, stale_updated_at = await _ready_order(allocation_fixture, quantity=1)
    with pytest.raises(OrderPreparationStale):
        await _prepare(
            allocation_fixture,
            stale_id,
            stale_updated_at - timedelta(microseconds=1),
            uuid.uuid4(),
        )

    async with allocation_fixture.factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.prepared")
        ) == 1


@pytest.mark.asyncio
async def test_competing_preparation_requests_commit_once(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, expected_updated_at = await _ready_order(allocation_fixture)
    results = await asyncio.gather(
        _prepare(allocation_fixture, order_id, expected_updated_at, uuid.uuid4()),
        _prepare(allocation_fixture, order_id, expected_updated_at, uuid.uuid4()),
        return_exceptions=True,
    )

    assert sum(isinstance(value, OrderPreparationResult) for value in results) == 1
    failure = next(value for value in results if isinstance(value, Exception))
    assert isinstance(failure, OrderPreparationStateConflict)
    async with allocation_fixture.factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.prepared")
        ) == 1


@pytest.mark.asyncio
async def test_concurrent_idempotent_replay_returns_one_committed_result(
    allocation_fixture: AllocationFixture,
) -> None:
    order_id, expected_updated_at = await _ready_order(allocation_fixture)
    key = uuid.uuid4()
    results = await asyncio.gather(
        _prepare(allocation_fixture, order_id, expected_updated_at, key),
        _prepare(allocation_fixture, order_id, expected_updated_at, key),
    )

    assert sorted(result.replayed for result in results) == [False, True]
    assert results[0].order.updated_at == results[1].order.updated_at
    async with allocation_fixture.factory() as session:
        assert await session.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.prepared")
        ) == 1


@pytest.mark.asyncio
async def test_preparation_rolls_back_order_and_audit_together(
    allocation_fixture: AllocationFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id, expected_updated_at = await _ready_order(allocation_fixture)

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr(order_fulfillment, "append_operator_audit_event", fail_audit)
    with pytest.raises(OrderPreparationUnavailable):
        await _prepare(
            allocation_fixture,
            order_id,
            expected_updated_at,
            uuid.uuid4(),
        )

    async with allocation_fixture.factory() as session:
        order = await session.get(Order, order_id)
        assert order.status == "confirmed"
        assert order.updated_at == expected_updated_at
        assert await session.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.prepared")
        ) == 0


def test_celery_writer_passes_guard_evidence_to_shared_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order_id = uuid.uuid4()
    key = uuid.uuid4()
    expected_updated_at = "2026-10-23T10:00:00+00:00"
    captured = {}

    class SessionContext:
        async def __aenter__(self):
            return AsyncMock()

        async def __aexit__(self, *args):
            return None

    async def shared_guard(session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(status="preparing")

    monkeypatch.setattr(
        "app.database.AsyncSessionLocal", lambda: SessionContext()
    )
    monkeypatch.setattr(
        "app.modules.m7_conversion.service.update_order_status", shared_guard
    )

    result = celery_update_order_status.run(
        order_id=str(order_id),
        new_status="preparing",
        idempotency_key=str(key),
        expected_updated_at=expected_updated_at,
    )

    assert result == {"order_id": str(order_id), "status": "preparing"}
    assert captured["order_id"] == order_id
    assert captured["idempotency_key"] == key
    assert captured["expected_updated_at"].isoformat() == expected_updated_at
    assert captured["actor_kind"] == "service"
