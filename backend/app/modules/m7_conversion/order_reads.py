"""Read-only Order summaries for application-layer composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.inventory import InventoryRecord
from app.models.lead import Lead
from app.models.order import Order
from app.models.order_delivery_issue import OrderDeliveryIssue
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.models.stock_movement import StockMovement
from app.modules.m7_conversion.order_fulfillment import (
    UnsupportedOrderPreparation,
    _allocation_state,
    _payment_state,
    _single_line,
)


@dataclass(frozen=True)
class CustomerOrderSummary:
    order_id: UUID
    status: str
    created_at: datetime


@dataclass(frozen=True)
class OrderQueueReadiness:
    """Compact, authoritative operational facts for one queue row."""

    payment_state: str
    stock_state: str
    inventory_state: str
    dispatch_state: str
    delivery_state: str
    issue_state: str


async def read_order_queue_readiness(
    db: AsyncSession,
    orders: list[Order],
) -> dict[UUID, OrderQueueReadiness]:
    """Project queue readiness with a fixed number of batched domain reads."""
    if not orders:
        return {}
    order_ids = [order.order_id for order in orders]
    payments = {
        payment.order_id: payment
        for payment in (
            await db.scalars(select(Payment).where(Payment.order_id.in_(order_ids)))
        ).all()
    }
    allocations = {
        allocation.order_id: allocation
        for allocation in (
            await db.scalars(
                select(StockAllocation).where(StockAllocation.order_id.in_(order_ids))
            )
        ).all()
    }
    movements = {
        movement.order_id: movement
        for movement in (
            await db.scalars(
                select(StockMovement).where(
                    StockMovement.order_id.in_(order_ids),
                    StockMovement.operation_kind == "dispatch",
                )
            )
        ).all()
        if movement.order_id is not None
    }
    line_facts: dict[UUID, tuple[UUID, int] | None] = {}
    sellable_item_ids: set[UUID] = set()
    for order in orders:
        try:
            line = _single_line(order)
            line_facts[order.order_id] = line
            sellable_item_ids.add(line[0])
        except UnsupportedOrderPreparation:
            line_facts[order.order_id] = None
    inventories = (
        {
            inventory.sellable_item_id: inventory
            for inventory in (
                await db.scalars(
                    select(InventoryRecord).where(
                        InventoryRecord.sellable_item_id.in_(sellable_item_ids)
                    )
                )
            ).all()
        }
        if sellable_item_ids
        else {}
    )
    open_issue_order_ids = set(
        (
            await db.scalars(
                select(OrderDeliveryIssue.order_id).where(
                    OrderDeliveryIssue.order_id.in_(order_ids),
                    OrderDeliveryIssue.status == "open",
                )
            )
        ).all()
    )

    readiness: dict[UUID, OrderQueueReadiness] = {}
    for order in orders:
        payment_state = _payment_state(payments.get(order.order_id), order)
        line = line_facts[order.order_id]
        movement = movements.get(order.order_id)
        if line is None:
            stock_state = "unsupported"
            inventory_state = "unsupported"
            dispatch_state = "missing"
        else:
            sellable_item_id, quantity = line
            allocation_state = _allocation_state(
                allocations.get(order.order_id),
                sellable_item_id=sellable_item_id,
                quantity=quantity,
            )
            stock_state = (
                "consumed"
                if movement is not None
                else "full_unconsumed"
                if allocation_state == "full"
                else allocation_state
            )
            inventory = inventories.get(sellable_item_id)
            inventory_state = (
                "ready"
                if inventory is not None
                and inventory.quantity is not None
                and inventory.quantity >= quantity
                and inventory.allocated_quantity >= quantity
                else "conflict"
            )
            dispatch_state = (
                "matched"
                if movement is not None
                and movement.sellable_item_id == sellable_item_id
                and movement.dispatched_quantity == quantity
                else "missing"
            )
        delivery_state = (
            "delivering"
            if order.status == "delivering"
            else "delivered"
            if order.status == "delivered"
            else "not_delivering"
        )
        readiness[order.order_id] = OrderQueueReadiness(
            payment_state=payment_state,
            stock_state=stock_state,
            inventory_state=inventory_state,
            dispatch_state=dispatch_state,
            delivery_state=delivery_state,
            issue_state="open" if order.order_id in open_issue_order_ids else "none",
        )
    return readiness


async def verified_order_conversations(
    db: AsyncSession,
    order_ids: list[UUID],
) -> dict[UUID, UUID]:
    """Return only Order-to-conversation links verified across owned records."""
    if not order_ids:
        return {}
    rows = await db.execute(
        select(OrderDraft.order_id, OrderDraft.conversation_id)
        .join(Order, Order.order_id == OrderDraft.order_id)
        .join(Conversation, Conversation.conversation_id == OrderDraft.conversation_id)
        .join(Lead, Lead.lead_id == Order.lead_id)
        .where(
            OrderDraft.order_id.in_(order_ids),
            OrderDraft.status == "confirmed",
            Lead.conversation_id == OrderDraft.conversation_id,
            Lead.customer_id == Order.customer_id,
            Conversation.customer_id == Order.customer_id,
        )
    )
    return {order_id: conversation_id for order_id, conversation_id in rows.all()}


async def read_customer_order_summaries(
    db: AsyncSession,
    *,
    customer_id: str,
    limit: int = 5,
) -> list[CustomerOrderSummary]:
    """Read a bounded newest-first Order history for one authoritative customer."""
    orders = list(
        (
            await db.scalars(
                select(Order)
                .where(Order.customer_id == customer_id)
                .order_by(Order.created_at.desc(), Order.order_id.desc())
                .limit(limit)
            )
        ).all()
    )
    return [
        CustomerOrderSummary(
            order_id=order.order_id,
            status=order.status,
            created_at=order.created_at,
        )
        for order in orders
    ]
