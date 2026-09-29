"""Inventory-owned quantity reads and Administrator mutation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, SellableItem
from app.models.inventory import InventoryRecord
from app.models.stock_movement import StockMovement
from app.modules.commerce_admin import (
    CommerceAdminContext,
    require_commerce_administrator,
)
from app.operator_identity.audit import append_operator_audit_event

InventoryStatus = Literal["available", "out_of_stock", "unknown"]


def availability_from_quantity(quantity: int | None) -> InventoryStatus:
    if quantity is None:
        return "unknown"
    return "out_of_stock" if quantity == 0 else "available"


class InventoryNotFound(Exception):
    pass


class InventoryConflict(Exception):
    """The reviewed stock state or operation key conflicts with current state."""


class InventoryQuantityUnknown(Exception):
    """A receipt cannot be applied without a verified starting count."""


@dataclass(frozen=True)
class InventoryStatusResult:
    sellable_item_id: uuid.UUID
    configured: bool
    status: InventoryStatus
    quantity: int | None
    inventory_id: uuid.UUID | None
    updated_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_quantity(quantity: int | None) -> None:
    if quantity is not None and (type(quantity) is not int or not 0 <= quantity <= 2147483647):
        raise ValueError("quantity must be a non-negative integer or null")


async def _lock_item_and_inventory(
    session: AsyncSession, sellable_item_id: uuid.UUID
) -> InventoryRecord | None:
    # Every stock writer locks the parent first, including when no inventory row exists.
    item = await session.scalar(
        select(SellableItem)
        .where(SellableItem.sellable_item_id == sellable_item_id)
        .with_for_update()
    )
    if item is None:
        raise InventoryNotFound("sellable item was not found")
    return await session.scalar(
        select(InventoryRecord)
        .where(InventoryRecord.sellable_item_id == sellable_item_id)
        .with_for_update()
    )


async def _existing_operation(
    session: AsyncSession, *, sellable_item_id: uuid.UUID, operation_key: uuid.UUID
) -> StockMovement | None:
    return await session.scalar(
        select(StockMovement).where(
            StockMovement.sellable_item_id == sellable_item_id,
            StockMovement.operation_key == operation_key,
        )
    )


def _assert_reviewed_state(
    record: InventoryRecord | None, *, expected_quantity: int | None,
    expected_updated_at: datetime | None,
) -> None:
    current_quantity = None if record is None else record.quantity
    current_updated_at = None if record is None else record.updated_at
    if current_quantity != expected_quantity or current_updated_at != expected_updated_at:
        raise InventoryConflict("stock changed since it was reviewed")


async def _persist_change(
    session: AsyncSession, *, sellable_item_id: uuid.UUID,
    record: InventoryRecord | None, quantity: int | None,
    operation_kind: Literal["receive", "adjust"], received_amount: int | None,
    reason: str | None, operation_key: uuid.UUID | None,
    actor, administrator: CommerceAdminContext, event_time: datetime,
    audit_action: str,
) -> tuple[InventoryRecord, StockMovement]:
    previous_quantity = None if record is None else record.quantity
    previous_updated_at = None if record is None else record.updated_at
    previous_status = "not_configured" if record is None else availability_from_quantity(record.quantity)
    status = availability_from_quantity(quantity)
    if previous_updated_at is not None and event_time <= previous_updated_at:
        event_time = previous_updated_at + timedelta(microseconds=1)
    if record is None:
        record = InventoryRecord(
            sellable_item_id=sellable_item_id, status=status,
            quantity=quantity, updated_at=event_time,
        )
        session.add(record)
    else:
        record.status = status
        record.quantity = quantity
        record.updated_at = event_time
    await session.flush()
    movement = StockMovement(
        sellable_item_id=sellable_item_id,
        operation_kind=operation_kind,
        before_quantity=previous_quantity,
        before_updated_at=previous_updated_at,
        received_amount=received_amount,
        corrected_quantity=quantity if operation_kind == "adjust" else None,
        after_quantity=quantity,
        reason=reason,
        actor_account_id=actor.account_id,
        operation_key=operation_key,
        occurred_at=event_time,
    )
    session.add(movement)
    await session.flush()
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action=audit_action,
        target_type="inventory_status",
        target_id=str(record.inventory_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={
            "sellable_item_id": str(sellable_item_id),
            "movement_id": str(movement.movement_id),
            "operation_kind": operation_kind,
            "previous_status": previous_status,
            "new_status": status,
            "previous_quantity": previous_quantity,
            "new_quantity": quantity,
        },
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return record, movement


async def get_inventory_status(
    session: AsyncSession, sellable_item_id: uuid.UUID
) -> InventoryStatusResult:
    record = await session.scalar(
        select(InventoryRecord).where(
            InventoryRecord.sellable_item_id == sellable_item_id
        )
    )
    if record is None:
        return InventoryStatusResult(
            sellable_item_id=sellable_item_id,
            configured=False,
            status="unknown",
            quantity=None,
            inventory_id=None,
            updated_at=None,
        )
    return InventoryStatusResult(
        sellable_item_id=sellable_item_id,
        configured=record.quantity is not None,
        status=availability_from_quantity(record.quantity),
        quantity=record.quantity,
        inventory_id=record.inventory_id,
        updated_at=record.updated_at,
    )


async def set_inventory_quantity(
    session: AsyncSession,
    *,
    sellable_item_id: uuid.UUID,
    quantity: int | None,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> InventoryRecord:
    _validate_quantity(quantity)
    actor = await require_commerce_administrator(session, administrator)
    record = await _lock_item_and_inventory(session, sellable_item_id)
    event_time = now or _utcnow()
    updated, _ = await _persist_change(
        session, sellable_item_id=sellable_item_id, record=record,
        quantity=quantity, operation_kind="adjust", received_amount=None,
        reason="Legacy absolute quantity update", operation_key=None,
        actor=actor, administrator=administrator, event_time=event_time,
        audit_action="commerce.inventory_quantity.changed",
    )
    return updated


async def receive_stock(
    session: AsyncSession, *, sellable_item_id: uuid.UUID,
    received_amount: int, expected_quantity: int,
    expected_updated_at: datetime | None, operation_key: uuid.UUID,
    administrator: CommerceAdminContext,
) -> StockMovement:
    if type(received_amount) is not int or not 1 <= received_amount <= 2147483647:
        raise ValueError("received amount must be a positive integer")
    if type(expected_quantity) is not int:
        raise ValueError("a reviewed stock quantity is required")
    _validate_quantity(expected_quantity)
    actor = await require_commerce_administrator(session, administrator)
    record = await _lock_item_and_inventory(session, sellable_item_id)
    existing = await _existing_operation(
        session, sellable_item_id=sellable_item_id, operation_key=operation_key
    )
    if existing is not None:
        if (existing.operation_kind != "receive" or existing.actor_account_id != actor.account_id
                or existing.received_amount != received_amount
                or existing.before_quantity != expected_quantity
                or existing.before_updated_at != expected_updated_at):
            raise InventoryConflict("operation key was used for another stock change")
        return existing
    if record is None or record.quantity is None:
        raise InventoryQuantityUnknown("establish a verified count before receiving stock")
    _assert_reviewed_state(
        record, expected_quantity=expected_quantity, expected_updated_at=expected_updated_at
    )
    new_quantity = record.quantity + received_amount
    _validate_quantity(new_quantity)
    _, movement = await _persist_change(
        session, sellable_item_id=sellable_item_id, record=record,
        quantity=new_quantity, operation_kind="receive", received_amount=received_amount,
        reason=None, operation_key=operation_key, actor=actor,
        administrator=administrator, event_time=_utcnow(),
        audit_action="commerce.stock.received",
    )
    return movement


async def adjust_stock(
    session: AsyncSession, *, sellable_item_id: uuid.UUID,
    corrected_quantity: int, reason: str,
    expected_quantity: int | None, expected_updated_at: datetime | None,
    operation_key: uuid.UUID, administrator: CommerceAdminContext,
) -> StockMovement:
    if type(corrected_quantity) is not int:
        raise ValueError("corrected quantity must be a non-negative integer")
    _validate_quantity(corrected_quantity)
    _validate_quantity(expected_quantity)
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 500:
        raise ValueError("a correction reason is required")
    normalized_reason = reason.strip()
    actor = await require_commerce_administrator(session, administrator)
    record = await _lock_item_and_inventory(session, sellable_item_id)
    existing = await _existing_operation(
        session, sellable_item_id=sellable_item_id, operation_key=operation_key
    )
    if existing is not None:
        if (existing.operation_kind != "adjust" or existing.actor_account_id != actor.account_id
                or existing.corrected_quantity != corrected_quantity
                or existing.before_quantity != expected_quantity
                or existing.before_updated_at != expected_updated_at
                or existing.reason != normalized_reason):
            raise InventoryConflict("operation key was used for another stock change")
        return existing
    _assert_reviewed_state(
        record, expected_quantity=expected_quantity, expected_updated_at=expected_updated_at
    )
    _, movement = await _persist_change(
        session, sellable_item_id=sellable_item_id, record=record,
        quantity=corrected_quantity, operation_kind="adjust", received_amount=None,
        reason=normalized_reason, operation_key=operation_key, actor=actor,
        administrator=administrator, event_time=_utcnow(),
        audit_action="commerce.stock.adjusted",
    )
    return movement


async def list_stock_movements(
    session: AsyncSession, *, sellable_item_id: uuid.UUID, limit: int = 20,
) -> list[StockMovement]:
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    item = await session.get(SellableItem, sellable_item_id)
    if item is None:
        raise InventoryNotFound("sellable item was not found")
    return list((await session.scalars(
        select(StockMovement)
        .where(StockMovement.sellable_item_id == sellable_item_id)
        .order_by(StockMovement.occurred_at.desc(), StockMovement.movement_id.desc())
        .limit(limit)
    )).all())


async def search_stock_items(
    session: AsyncSession, *, query: str | None, sellable_item_id: uuid.UUID | None,
    operational_only: bool, limit: int,
) -> tuple[list[tuple[Product, SellableItem, InventoryRecord | None]], bool]:
    """One bounded database read for exact variant identity and stock state."""
    if not 1 <= limit <= 50:
        raise ValueError("limit must be between 1 and 50")
    statement = (
        select(Product, SellableItem, InventoryRecord)
        .join(SellableItem, SellableItem.product_id == Product.product_id)
        .outerjoin(InventoryRecord, InventoryRecord.sellable_item_id == SellableItem.sellable_item_id)
    )
    if operational_only:
        statement = statement.where(Product.active.is_(True), SellableItem.active.is_(True))
    if sellable_item_id is not None:
        statement = statement.where(SellableItem.sellable_item_id == sellable_item_id)
    elif query:
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        statement = statement.where(or_(
            Product.name.ilike(pattern, escape="\\"),
            SellableItem.model_label.ilike(pattern, escape="\\"),
            SellableItem.sku.ilike(pattern, escape="\\"),
        ))
    rows = (await session.execute(
        statement.order_by(Product.name, SellableItem.model_label, SellableItem.sellable_item_id)
        .limit(limit + 1)
    )).all()
    return [(row[0], row[1], row[2]) for row in rows[:limit]], len(rows) > limit
