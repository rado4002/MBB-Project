"""Catalog-owned queries and Administrator mutations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, SellableItem
from app.modules.commerce_admin import (
    CommerceAdminContext,
    require_commerce_administrator,
)
from app.operator_identity.audit import append_operator_audit_event

UNSET = object()


class CatalogNotFound(Exception):
    pass


class CatalogConflict(Exception):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def list_products(session: AsyncSession, *, limit: int = 200) -> list[Product]:
    return list(
        (
            await session.scalars(
                select(Product).order_by(Product.name, Product.product_id).limit(limit)
            )
        ).all()
    )


async def get_product(session: AsyncSession, product_id: uuid.UUID) -> Product | None:
    return await session.get(Product, product_id)


async def list_sellable_items(
    session: AsyncSession, *, product_id: uuid.UUID | None = None, limit: int = 200
) -> list[SellableItem]:
    statement = select(SellableItem)
    if product_id is not None:
        statement = statement.where(SellableItem.product_id == product_id)
    return list(
        (
            await session.scalars(
                statement.order_by(
                    SellableItem.product_id,
                    SellableItem.model_label,
                    SellableItem.sellable_item_id,
                ).limit(limit)
            )
        ).all()
    )


async def get_sellable_item(
    session: AsyncSession, sellable_item_id: uuid.UUID
) -> SellableItem | None:
    return await session.get(SellableItem, sellable_item_id)


async def create_product(
    session: AsyncSession,
    *,
    name: str,
    category_code: str,
    description: str,
    active: bool,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> Product:
    actor = await require_commerce_administrator(session, administrator)
    event_time = now or _utcnow()
    product = Product(
        name=name,
        category_code=category_code,
        description=description,
        active=active,
        created_at=event_time,
        updated_at=event_time,
    )
    session.add(product)
    await session.flush()
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.product.created",
        target_type="product",
        target_id=str(product.product_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={"category_code": product.category_code, "active": product.active},
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return product


async def update_product(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    administrator: CommerceAdminContext,
    name: str | object = UNSET,
    category_code: str | object = UNSET,
    description: str | object = UNSET,
    active: bool | object = UNSET,
    now: datetime | None = None,
) -> Product:
    actor = await require_commerce_administrator(session, administrator)
    product = await session.scalar(
        select(Product).where(Product.product_id == product_id).with_for_update()
    )
    if product is None:
        raise CatalogNotFound("product was not found")
    changed: list[str] = []
    previous_active = product.active
    for field, value in (
        ("name", name),
        ("category_code", category_code),
        ("description", description),
        ("active", active),
    ):
        if value is not UNSET and getattr(product, field) != value:
            setattr(product, field, value)
            changed.append(field)
    event_time = now or _utcnow()
    product.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.product.updated",
        target_type="product",
        target_id=str(product.product_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={
            "changed_fields": changed,
            "previous_active": previous_active,
            "new_active": product.active,
        },
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return product


async def create_sellable_item(
    session: AsyncSession,
    *,
    product_id: uuid.UUID,
    model_label: str | None,
    sku: str | None,
    attributes: dict[str, str | int | bool],
    active: bool,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> SellableItem:
    actor = await require_commerce_administrator(session, administrator)
    product = await session.scalar(
        select(Product).where(Product.product_id == product_id).with_for_update()
    )
    if product is None:
        raise CatalogNotFound("product was not found")
    event_time = now or _utcnow()
    item = SellableItem(
        product_id=product_id,
        model_label=model_label,
        sku=sku,
        attributes=attributes,
        active=active,
        created_at=event_time,
        updated_at=event_time,
    )
    try:
        async with session.begin_nested():
            session.add(item)
            await session.flush()
    except IntegrityError as exc:
        raise CatalogConflict("sellable item SKU already exists") from exc
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.sellable_item.created",
        target_type="sellable_item",
        target_id=str(item.sellable_item_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={"product_id": str(product_id), "active": item.active},
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return item


async def update_sellable_item(
    session: AsyncSession,
    *,
    sellable_item_id: uuid.UUID,
    administrator: CommerceAdminContext,
    model_label: str | None | object = UNSET,
    sku: str | None | object = UNSET,
    attributes: dict[str, str | int | bool] | object = UNSET,
    active: bool | object = UNSET,
    now: datetime | None = None,
) -> SellableItem:
    actor = await require_commerce_administrator(session, administrator)
    item = await session.scalar(
        select(SellableItem)
        .where(SellableItem.sellable_item_id == sellable_item_id)
        .with_for_update()
    )
    if item is None:
        raise CatalogNotFound("sellable item was not found")
    changed: list[str] = []
    previous_active = item.active
    values: tuple[tuple[str, Any], ...] = (
        ("model_label", model_label),
        ("sku", sku),
        ("attributes", attributes),
        ("active", active),
    )
    for field, value in values:
        if value is not UNSET and getattr(item, field) != value:
            setattr(item, field, value)
            changed.append(field)
    event_time = now or _utcnow()
    item.updated_at = event_time
    try:
        async with session.begin_nested():
            await session.flush()
    except IntegrityError as exc:
        raise CatalogConflict("sellable item SKU already exists") from exc
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.sellable_item.updated",
        target_type="sellable_item",
        target_id=str(item.sellable_item_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={
            "changed_fields": changed,
            "previous_active": previous_active,
            "new_active": item.active,
        },
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return item
