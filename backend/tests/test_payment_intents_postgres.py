"""Disposable PostgreSQL evidence for provider-independent Payment intents."""

from __future__ import annotations

import asyncio
import os
import uuid
from dataclasses import dataclass

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.modules.m7_conversion.payment_intents import (
    PaymentIntentAcceptanceRequired,
    PaymentIntentConflict,
    PaymentIntentIdempotencyConflict,
    PaymentIntentStateConflict,
    UnsupportedPaymentIntent,
    create_payment_intent,
)
from app.schemas.common import PaymentMethod
from test_order_drafts_postgres import _prepare, _reply, _seed

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="Disposable PostgreSQL required"
)

TRUNCATE = text(
    "TRUNCATE TABLE mbb.customers, mbb.products, mbb.exchange_rates "
    "RESTART IDENTITY CASCADE"
)


@dataclass(frozen=True)
class AcceptedOrderFixture:
    factory: async_sessionmaker
    order_id: uuid.UUID
    customer_id: str


@pytest_asyncio.fixture(loop_scope="function")
async def accepted_order() -> AcceptedOrderFixture:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.execute(TRUNCATE)
    seeded = await _seed(factory)
    prepared = await _prepare(factory, seeded)
    async with factory() as session:
        draft = await session.get(
            OrderDraft, (prepared.draft_id, prepared.draft_version)
        )
        confirmation = f"OUI {draft.confirmation_code}"
    accepted, _ = await _reply(factory, seeded, content=confirmation)
    assert accepted is not None and accepted.order_id is not None
    async with factory() as session:
        order = await session.get(Order, accepted.order_id)
        assert order is not None
        value = AcceptedOrderFixture(
            factory=factory,
            order_id=order.order_id,
            customer_id=order.customer_id,
        )
    try:
        yield value
    finally:
        async with engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await engine.dispose()


async def _create(
    fixture: AcceptedOrderFixture,
    *,
    method: PaymentMethod,
    key: uuid.UUID,
):
    async with fixture.factory() as session:
        try:
            result = await create_payment_intent(
                session,
                order_id=fixture.order_id,
                method=method,
                idempotency_key=key,
            )
            await session.commit()
            return result
        except Exception:
            await session.rollback()
            raise


@pytest.mark.asyncio
async def test_create_uses_accepted_order_snapshot_without_side_effects(
    accepted_order: AcceptedOrderFixture,
) -> None:
    key = uuid.uuid4()
    result = await _create(
        accepted_order, method=PaymentMethod.orange_money, key=key
    )
    assert result.replayed is False
    assert result.verification_state == "unverified"
    assert result.customer_id == accepted_order.customer_id
    payment_id = result.payment.payment_id

    async with accepted_order.factory() as session:
        order = await session.get(Order, accepted_order.order_id)
        payment = await session.get(Payment, payment_id)
        assert payment is not None and order is not None
        assert payment.order_id == order.order_id
        assert payment.customer_id == order.customer_id
        assert payment.method == "orange_money"
        assert payment.amount == order.total_amount
        assert payment.currency == order.currency
        assert payment.status == "pending"
        assert payment.completed_at is None
        assert payment.provider_transaction_id is None
        assert payment.provider_response is None
        assert payment.failure_reason is None
        assert len(payment.intent_idempotency_key_digest or "") == 64
        assert len(payment.intent_request_fingerprint or "") == 64
        assert order.status == "pending" and order.confirmed_at is None
        assert await session.scalar(
            select(func.count()).select_from(StockAllocation)
        ) == 0


@pytest.mark.asyncio
async def test_replay_and_conflicting_method_are_deterministic(
    accepted_order: AcceptedOrderFixture,
) -> None:
    key = uuid.uuid4()
    first = await _create(
        accepted_order, method=PaymentMethod.airtel_money, key=key
    )
    replay = await _create(
        accepted_order, method=PaymentMethod.airtel_money, key=key
    )
    alternate_key_replay = await _create(
        accepted_order, method=PaymentMethod.airtel_money, key=uuid.uuid4()
    )
    assert first.payment.payment_id == replay.payment.payment_id
    assert replay.replayed is True and alternate_key_replay.replayed is True

    with pytest.raises(PaymentIntentIdempotencyConflict):
        await _create(accepted_order, method=PaymentMethod.mpesa, key=key)
    with pytest.raises(PaymentIntentConflict):
        await _create(
            accepted_order, method=PaymentMethod.mpesa, key=uuid.uuid4()
        )
    async with accepted_order.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_eligibility_and_accepted_snapshot_fail_closed(
    accepted_order: AcceptedOrderFixture,
) -> None:
    async with accepted_order.factory() as session:
        with pytest.raises(UnsupportedPaymentIntent):
            await create_payment_intent(
                session,
                order_id=accepted_order.order_id,
                method=None,  # type: ignore[arg-type]
                idempotency_key=uuid.uuid4(),
            )
    async with accepted_order.factory() as session:
        order = await session.get(Order, accepted_order.order_id)
        order.status = "confirmed"
        await session.commit()
    with pytest.raises(PaymentIntentStateConflict):
        await _create(
            accepted_order, method=PaymentMethod.bank_transfer, key=uuid.uuid4()
        )

    async with accepted_order.factory() as session:
        order = await session.get(Order, accepted_order.order_id)
        order.status = "pending"
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.order_id == accepted_order.order_id)
        )
        draft.order_id = None
        await session.commit()
    with pytest.raises(PaymentIntentAcceptanceRequired):
        await _create(
            accepted_order, method=PaymentMethod.bank_transfer, key=uuid.uuid4()
        )

    async with accepted_order.factory() as session:
        order = await session.get(Order, accepted_order.order_id)
        order.items = [*order.items, dict(order.items[0])]
        await session.commit()
    with pytest.raises(UnsupportedPaymentIntent):
        await _create(
            accepted_order, method=PaymentMethod.bank_transfer, key=uuid.uuid4()
        )
    async with accepted_order.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0


@pytest.mark.asyncio
async def test_caller_rollback_removes_intent_and_allows_retry(
    accepted_order: AcceptedOrderFixture,
) -> None:
    key = uuid.uuid4()
    async with accepted_order.factory() as session:
        result = await create_payment_intent(
            session,
            order_id=accepted_order.order_id,
            method=PaymentMethod.cash,
            idempotency_key=key,
        )
        rolled_back_id = result.payment.payment_id
        await session.rollback()
    async with accepted_order.factory() as session:
        assert await session.get(Payment, rolled_back_id) is None
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0

    retry = await _create(accepted_order, method=PaymentMethod.cash, key=key)
    assert retry.replayed is False and retry.payment.payment_id != rolled_back_id


@pytest.mark.asyncio
async def test_concurrent_replay_creates_exactly_one_intent(
    accepted_order: AcceptedOrderFixture,
) -> None:
    key = uuid.uuid4()
    first, second = await asyncio.gather(
        _create(accepted_order, method=PaymentMethod.mpesa, key=key),
        _create(accepted_order, method=PaymentMethod.mpesa, key=key),
    )
    assert first.payment.payment_id == second.payment.payment_id
    assert sorted((first.replayed, second.replayed)) == [False, True]
    async with accepted_order.factory() as session:
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1


@pytest.mark.asyncio
async def test_concurrent_different_methods_commit_one_and_conflict_one(
    accepted_order: AcceptedOrderFixture,
) -> None:
    results = await asyncio.gather(
        _create(
            accepted_order,
            method=PaymentMethod.orange_money,
            key=uuid.uuid4(),
        ),
        _create(
            accepted_order,
            method=PaymentMethod.bank_transfer,
            key=uuid.uuid4(),
        ),
        return_exceptions=True,
    )
    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, PaymentIntentConflict) for result in results) == 1
    async with accepted_order.factory() as session:
        payment = await session.scalar(select(Payment))
        assert payment is not None and payment.status == "pending"
        assert payment.method in {"orange_money", "bank_transfer"}
        assert await session.scalar(select(func.count()).select_from(Payment)) == 1
