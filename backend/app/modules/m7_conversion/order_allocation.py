"""Paid Order validation and explicit Inventory allocation orchestration."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.modules.inventory.service import (
    InventoryAllocationConflict,
    allocate_order_quantity,
    get_order_stock_allocation,
)


class OrderStockAllocationError(Exception):
    """Base error for stable Order allocation failures."""


class OrderStockAllocationNotFound(OrderStockAllocationError):
    pass


class OrderStockAllocationStateConflict(OrderStockAllocationError):
    pass


class OrderStockAllocationPaymentRequired(OrderStockAllocationError):
    pass


class UnsupportedOrderStockAllocation(OrderStockAllocationError):
    pass


class OrderStockAllocationConflict(OrderStockAllocationError):
    pass


@dataclass(frozen=True)
class OrderStockAllocationResult:
    allocation: StockAllocation
    replayed: bool


def _single_order_line(order: Order) -> tuple[uuid.UUID, int]:
    if not isinstance(order.items, list) or len(order.items) != 1:
        raise UnsupportedOrderStockAllocation(
            "Stock allocation currently supports exactly one Order line"
        )
    line = order.items[0]
    if not isinstance(line, dict):
        raise UnsupportedOrderStockAllocation("Order line is invalid")
    quantity = line.get("quantity")
    if type(quantity) is not int or quantity <= 0:
        raise UnsupportedOrderStockAllocation("Order line quantity is invalid")
    try:
        sellable_item_id = uuid.UUID(str(line["sellable_item_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedOrderStockAllocation(
            "Order line has no valid Sellable Item identity"
        ) from exc
    return sellable_item_id, quantity


async def allocate_paid_order_stock(
    session: AsyncSession, *, order_id: uuid.UUID
) -> OrderStockAllocationResult:
    """Allocate the complete quantity for one explicitly paid single-line Order.

    This transaction performs no provider call and no fulfillment transition.
    The caller controls commit or rollback.
    """
    order = await session.scalar(
        select(Order)
        .where(Order.order_id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise OrderStockAllocationNotFound("Order was not found")
    sellable_item_id, quantity = _single_order_line(order)

    existing = await get_order_stock_allocation(session, order_id=order_id)
    if existing is not None:
        if (
            existing.sellable_item_id != sellable_item_id
            or existing.quantity != quantity
        ):
            raise OrderStockAllocationConflict(
                "Order contents conflict with its committed Stock allocation"
            )
        return OrderStockAllocationResult(allocation=existing, replayed=True)

    if order.status != "confirmed":
        raise OrderStockAllocationStateConflict(
            "Order must be confirmed before Stock allocation"
        )
    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order_id).with_for_update()
    )
    if (
        payment is None
        or payment.status != "completed"
        or payment.completed_at is None
        or payment.amount != order.total_amount
        or payment.currency != order.currency
    ):
        raise OrderStockAllocationPaymentRequired(
            "a matching completed Payment is required before Stock allocation"
        )
    try:
        allocation = await allocate_order_quantity(
            session,
            order_id=order_id,
            sellable_item_id=sellable_item_id,
            quantity=quantity,
        )
    except InventoryAllocationConflict as exc:
        raise OrderStockAllocationConflict(str(exc)) from exc
    return OrderStockAllocationResult(allocation=allocation, replayed=False)
