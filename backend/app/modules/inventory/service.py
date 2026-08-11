"""Inventory-owned status reads and Administrator mutation."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import SellableItem
from app.models.inventory import InventoryRecord
from app.modules.commerce_admin import (
    CommerceAdminContext,
    require_commerce_administrator,
)
from app.operator_identity.audit import append_operator_audit_event

InventoryStatus = Literal["available", "out_of_stock", "unknown"]
ALLOWED_STATUSES = frozenset({"available", "out_of_stock", "unknown"})


class InventoryNotFound(Exception):
    pass


@dataclass(frozen=True)
class InventoryStatusResult:
    sellable_item_id: uuid.UUID
    configured: bool
    status: InventoryStatus
    inventory_id: uuid.UUID | None
    updated_at: datetime | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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
            inventory_id=None,
            updated_at=None,
        )
    return InventoryStatusResult(
        sellable_item_id=sellable_item_id,
        configured=True,
        status=record.status,  # type: ignore[arg-type]
        inventory_id=record.inventory_id,
        updated_at=record.updated_at,
    )


async def set_inventory_status(
    session: AsyncSession,
    *,
    sellable_item_id: uuid.UUID,
    status: InventoryStatus,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> InventoryRecord:
    if status not in ALLOWED_STATUSES:
        raise ValueError("unsupported inventory status")
    actor = await require_commerce_administrator(session, administrator)
    item = await session.scalar(
        select(SellableItem)
        .where(SellableItem.sellable_item_id == sellable_item_id)
        .with_for_update()
    )
    if item is None:
        raise InventoryNotFound("sellable item was not found")
    event_time = now or _utcnow()
    record = await session.scalar(
        select(InventoryRecord)
        .where(InventoryRecord.sellable_item_id == sellable_item_id)
        .with_for_update()
    )
    previous_status = "not_configured" if record is None else record.status
    if record is None:
        record = InventoryRecord(
            sellable_item_id=sellable_item_id,
            status=status,
            updated_at=event_time,
        )
        session.add(record)
    else:
        record.status = status
        record.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.inventory_status.changed",
        target_type="inventory_status",
        target_id=str(record.inventory_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={
            "sellable_item_id": str(sellable_item_id),
            "previous_status": previous_status,
            "new_status": record.status,
        },
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return record
