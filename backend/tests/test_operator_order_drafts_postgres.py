"""Connected evidence for operator Product Offer -> OrderDraft -> confirmation."""

# ruff: noqa: F811

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.models import Message, OperatorAccount
from app.models.catalog import SellableItem
from app.models.conversation import Conversation
from app.models.inventory import InventoryRecord
from app.models.operator_audit import OperatorAuditEvent
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.modules.m7_conversion.order_drafts import (
    OperatorOrderDraftActiveConflict,
    OperatorOrderDraftIdempotencyConflict,
    OperatorOrderDraftInsufficientStock,
    OperatorOrderDraftStaleOffer,
    handle_order_draft_reply,
    prepare_operator_order_draft,
)
from app.operator_identity.passwords import hash_password
from test_order_drafts_postgres import (  # noqa: F401
    _block_m1_external_effects,
    _seed,
    engine as postgres_engine,
)


DATABASE_URL = os.environ.get("AI6B_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI6B_TEST_DATABASE_URL is required for operator OrderDraft evidence",
)


async def _operator_context(factory, seeded):
    now = datetime.now(timezone.utc)
    operator = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized=f"draft.operator.{uuid.uuid4().hex[:8]}",
        display_name="Draft Operator",
        password_hash=hash_password("Operator-Draft-7329!"),
        role="operator",
        status="active",
        must_change_password=False,
        password_changed_at=now,
    )
    async with factory() as session:
        session.add(operator)
        conversation = await session.get(Conversation, seeded.conversation_id)
        assert conversation is not None
        conversation.owner_type = "human"
        conversation.human_owner_account_id = operator.account_id
        conversation.ai_execution_state = "paused"
        conversation.ownership_version = 2
        await session.commit()
    return operator


async def _request_values(factory, seeded):
    async with factory() as session:
        item = await session.get(SellableItem, seeded.sellable_item_id)
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        from app.modules.product_offer.service import get_product_offer

        offer = await get_product_offer(session, seeded.sellable_item_id)
        assert item is not None and inventory is not None and offer is not None
        assert offer.price_id is not None and offer.derived_cdf_quote is not None
        assert offer.inventory_updated_at is not None
        return {
            "product_id": item.product_id,
            "sellable_item_id": seeded.sellable_item_id,
            "price_id": offer.price_id,
            "exchange_rate_id": offer.derived_cdf_quote.exchange_rate_id,
            "inventory_updated_at": offer.inventory_updated_at,
        }


async def _prepare(factory, seeded, operator, *, key=None, quantity=2, **overrides):
    values = await _request_values(factory, seeded)
    values.update(overrides)
    async with factory() as session:
        return await prepare_operator_order_draft(
            session,
            conversation_id=seeded.conversation_id,
            expected_ownership_version=2,
            quantity=quantity,
            idempotency_key=key or uuid.uuid4(),
            actor_account_id=operator.account_id,
            actor_display_name=operator.display_name,
            actor_role=operator.role,
            request_id="operator-draft-test",
            source_network_fingerprint=None,
            user_agent_fingerprint=None,
            **values,
        )


@pytest.mark.asyncio
async def test_operator_prepare_is_unsent_idempotent_and_only_customer_confirms(postgres_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    seeded = await _seed(factory)
    operator = await _operator_context(factory, seeded)
    key = uuid.uuid4()
    async with factory() as session:
        before_messages = await session.scalar(select(func.count()).select_from(Message))
        before_inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert before_inventory is not None
        inventory_snapshot = (
            before_inventory.quantity,
            before_inventory.allocated_quantity,
            before_inventory.updated_at,
        )

    prepared, replay = await asyncio.gather(
        _prepare(factory, seeded, operator, key=key),
        _prepare(factory, seeded, operator, key=key),
    )
    if prepared.replayed:
        prepared, replay = replay, prepared
    assert prepared.replayed is False and replay.replayed is True
    assert replay.draft.draft_id == prepared.draft.draft_id
    assert replay.customer_confirmation_text == prepared.customer_confirmation_text

    async with factory() as session:
        draft = await session.get(OrderDraft, (prepared.draft.draft_id, 1))
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert draft is not None and draft.status == "awaiting_confirmation"
        assert draft.presented_outbound_message_id is None
        assert draft.created_by_turn_id is None
        assert draft.created_by_operator_account_id == operator.account_id
        assert await session.scalar(select(func.count()).select_from(Message)) == before_messages
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert inventory is not None
        assert (inventory.quantity, inventory.allocated_quantity, inventory.updated_at) == inventory_snapshot
        audit = await session.scalar(select(OperatorAuditEvent).where(
            OperatorAuditEvent.action == "order_draft.prepared"
        ))
        assert audit is not None and audit.actor_account_id == operator.account_id

    inbound_id = uuid.uuid4()
    async with factory() as session:
        session.add(Message(
            message_id=inbound_id,
            conversation_id=seeded.conversation_id,
            timestamp=datetime.now(timezone.utc),
            direction="inbound",
            content=f"OUI {prepared.draft.confirmation_code}",
            content_type="text",
            language="french",
            whatsapp_message_id=f"operator-confirm-{inbound_id}",
        ))
        await session.flush()
        result = await handle_order_draft_reply(
            session,
            conversation_id=seeded.conversation_id,
            source_message_id=inbound_id,
            expected_ownership_version=2,
            customer_text=f"OUI {prepared.draft.confirmation_code}",
        )
        assert result is not None and result.state == "confirmed"
        assert result.order_id is not None
        await session.commit()

    async with factory() as session:
        order = await session.get(Order, result.order_id)
        draft = await session.get(OrderDraft, (prepared.draft.draft_id, 1))
        inventory = await session.get(InventoryRecord, seeded.inventory_id)
        assert order is not None and order.status == "pending"
        assert draft is not None and draft.status == "confirmed"
        assert draft.order_id == order.order_id
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
        assert inventory is not None
        assert (inventory.quantity, inventory.allocated_quantity, inventory.updated_at) == inventory_snapshot


@pytest.mark.asyncio
async def test_operator_prepare_rejects_stale_offer_stock_conflicts_and_key_reuse(postgres_engine):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    seeded = await _seed(factory)
    operator = await _operator_context(factory, seeded)
    with pytest.raises(OperatorOrderDraftStaleOffer):
        await _prepare(
            factory, seeded, operator,
            price_id=uuid.uuid4(),
        )
    with pytest.raises(OperatorOrderDraftInsufficientStock):
        await _prepare(factory, seeded, operator, quantity=6)

    key = uuid.uuid4()
    await _prepare(factory, seeded, operator, key=key, quantity=2)
    with pytest.raises(OperatorOrderDraftIdempotencyConflict):
        await _prepare(factory, seeded, operator, key=key, quantity=3)
    with pytest.raises(OperatorOrderDraftActiveConflict):
        await _prepare(factory, seeded, operator, quantity=3)


@pytest.mark.asyncio
async def test_m1_resolves_operator_draft_before_human_control_gate(
    postgres_engine, monkeypatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.tasks import m1

    factory = async_sessionmaker(postgres_engine, expire_on_commit=False)
    seeded = await _seed(factory)
    operator = await _operator_context(factory, seeded)
    prepared = await _prepare(factory, seeded, operator)
    external_calls = _block_m1_external_effects(
        monkeypatch, factory, whatsapp_send_enabled=True
    )

    class TaskStub:
        request = SimpleNamespace(retries=0)

        def retry(self, **_kwargs):
            raise AssertionError("M1 unexpectedly requested a retry")

    async def confirm(message_id, provider_id):
        return await m1._process(
            task=TaskStub(),
            message_id=str(message_id),
            customer_phone="+243810006001",
            content=f"OUI {prepared.draft.confirmation_code}",
            content_type="text",
            timestamp=datetime.now(timezone.utc).isoformat(),
            whatsapp_message_id=provider_id,
        )

    first_id = uuid.uuid4()
    first = await confirm(first_id, f"operator-confirm-{first_id}")
    replay = await confirm(uuid.uuid4(), f"operator-confirm-replay-{first_id}")
    redelivery = await confirm(first_id, f"operator-confirm-{first_id}")

    assert first["status"] == "order_draft_confirmed"
    assert replay["status"] == "order_draft_already_confirmed"
    assert redelivery["status"] == "skipped"
    assert first["order_id"] == replay["order_id"]
    assert first["send_status"] == replay["send_status"] == "skipped"
    assert external_calls == []
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Order)) == 1
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
