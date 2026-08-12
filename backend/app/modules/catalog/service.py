"""Catalog-owned queries and Administrator mutations."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import (
MAX_PRODUCT_MEDIA_PER_OWNER,
    Product,
    ProductMedia,
    SellableItem,
)
from app.modules.commerce_admin import (
    CommerceAdminContext,
    require_commerce_administrator,
)
from app.operator_identity.audit import append_operator_audit_event

UNSET = object()
MAX_PRODUCT_MEDIA_ADMIN_LIST = 200


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


def _media_owner_filter(
    *, product_id: uuid.UUID | None, sellable_item_id: uuid.UUID | None
):
    if (product_id is None) == (sellable_item_id is None):
        raise ValueError("exactly one Product Media owner is required")
    if product_id is not None:
        return ProductMedia.product_id == product_id
    return ProductMedia.sellable_item_id == sellable_item_id


async def _lock_media_owner(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None,
    sellable_item_id: uuid.UUID | None,
) -> Product | SellableItem:
    _media_owner_filter(product_id=product_id, sellable_item_id=sellable_item_id)
    if product_id is not None:
        owner = await session.scalar(
            select(Product).where(Product.product_id == product_id).with_for_update()
        )
        if owner is None:
            raise CatalogNotFound("product was not found")
        return owner
    owner = await session.scalar(
        select(SellableItem)
        .where(SellableItem.sellable_item_id == sellable_item_id)
        .with_for_update()
    )
    if owner is None:
        raise CatalogNotFound("sellable item was not found")
    return owner


async def get_media(session: AsyncSession, media_id: uuid.UUID) -> ProductMedia | None:
    return await session.get(ProductMedia, media_id)


async def list_product_media(
    session: AsyncSession,
    product_id: uuid.UUID,
    *,
    active_only: bool = True,
) -> list[ProductMedia]:
    statement = select(ProductMedia).where(ProductMedia.product_id == product_id)
    if active_only:
        statement = statement.where(ProductMedia.active.is_(True))
    return list(
        (
            await session.scalars(
                statement.order_by(
                    ProductMedia.display_order,
                    ProductMedia.created_at,
                    ProductMedia.media_id,
                ).limit(
                    MAX_PRODUCT_MEDIA_PER_OWNER
                    if active_only
                    else MAX_PRODUCT_MEDIA_ADMIN_LIST
                )
            )
        ).all()
    )


async def list_sellable_item_media(
    session: AsyncSession,
    sellable_item_id: uuid.UUID,
    *,
    active_only: bool = True,
) -> list[ProductMedia]:
    statement = select(ProductMedia).where(
        ProductMedia.sellable_item_id == sellable_item_id
    )
    if active_only:
        statement = statement.where(ProductMedia.active.is_(True))
    return list(
        (
            await session.scalars(
                statement.order_by(
                    ProductMedia.display_order,
                    ProductMedia.created_at,
                    ProductMedia.media_id,
                ).limit(
                    MAX_PRODUCT_MEDIA_PER_OWNER
                    if active_only
                    else MAX_PRODUCT_MEDIA_ADMIN_LIST
                )
            )
        ).all()
    )


async def get_effective_primary_media(
    session: AsyncSession, sellable_item_id: uuid.UUID
) -> ProductMedia | None:
    item = await session.get(SellableItem, sellable_item_id)
    if item is None:
        raise CatalogNotFound("sellable item was not found")
    item_primary = await session.scalar(
        select(ProductMedia).where(
            ProductMedia.sellable_item_id == sellable_item_id,
            ProductMedia.active.is_(True),
            ProductMedia.is_primary.is_(True),
        )
    )
    if item_primary is not None:
        return item_primary
    return await session.scalar(
        select(ProductMedia).where(
            ProductMedia.product_id == item.product_id,
            ProductMedia.active.is_(True),
            ProductMedia.is_primary.is_(True),
        )
    )


async def _append_media_audit(
    session: AsyncSession,
    *,
    actor: Any,
    administrator: CommerceAdminContext,
    media: ProductMedia,
    action: str,
    metadata: dict[str, Any],
    occurred_at: datetime,
) -> None:
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action=action,
        target_type="product_media",
        target_id=str(media.media_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata=metadata,
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=occurred_at,
    )


async def create_product_media(
    session: AsyncSession,
    *,
    product_id: uuid.UUID | None,
    sellable_item_id: uuid.UUID | None,
    asset_url: str,
    alt_text: str | None,
    is_primary: bool,
    display_order: int,
    active: bool,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> ProductMedia:
    actor = await require_commerce_administrator(session, administrator)
    await _lock_media_owner(
        session, product_id=product_id, sellable_item_id=sellable_item_id
    )
    owner_filter = _media_owner_filter(
        product_id=product_id, sellable_item_id=sellable_item_id
    )
    if active:
        active_count = await session.scalar(
            select(func.count()).select_from(ProductMedia).where(
                owner_filter, ProductMedia.active.is_(True)
            )
        )
        if active_count >= MAX_PRODUCT_MEDIA_PER_OWNER:
            raise CatalogConflict("active Product Media limit was reached")
    if is_primary and not active:
        raise ValueError("primary media must be active")
    event_time = now or _utcnow()
    media = ProductMedia(
        product_id=product_id,
        sellable_item_id=sellable_item_id,
        asset_url=asset_url,
        alt_text=alt_text,
        is_primary=False,
        display_order=display_order,
        active=active,
        created_at=event_time,
        updated_at=event_time,
    )
    session.add(media)
    await session.flush()
    if is_primary:
        await session.execute(
            update(ProductMedia)
            .where(
                owner_filter,
                ProductMedia.active.is_(True),
                ProductMedia.is_primary.is_(True),
                ProductMedia.media_id != media.media_id,
            )
            .values(is_primary=False, updated_at=event_time)
        )
        media.is_primary = True
        await session.flush()
    await _append_media_audit(
        session,
        actor=actor,
        administrator=administrator,
        media=media,
        action="commerce.product_media.created",
        metadata={
            "owner_scope": "product" if product_id is not None else "sellable_item",
            "owner_id": str(product_id or sellable_item_id),
            "active": media.active,
            "is_primary": media.is_primary,
        },
        occurred_at=event_time,
    )
    return media


async def update_product_media(
    session: AsyncSession,
    *,
    media_id: uuid.UUID,
    administrator: CommerceAdminContext,
    asset_url: str | object = UNSET,
    alt_text: str | None | object = UNSET,
    display_order: int | object = UNSET,
    active: bool | object = UNSET,
    now: datetime | None = None,
) -> ProductMedia:
    actor = await require_commerce_administrator(session, administrator)
    existing = await session.get(ProductMedia, media_id)
    if existing is None:
        raise CatalogNotFound("Product Media was not found")
    await _lock_media_owner(
        session,
        product_id=existing.product_id,
        sellable_item_id=existing.sellable_item_id,
    )
    media = await session.scalar(
        select(ProductMedia)
        .where(ProductMedia.media_id == media_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if media is None:
        raise CatalogNotFound("Product Media was not found")
    if active is True and not media.active:
        owner_filter = _media_owner_filter(
            product_id=media.product_id, sellable_item_id=media.sellable_item_id
        )
        active_count = await session.scalar(
            select(func.count()).select_from(ProductMedia).where(
                owner_filter, ProductMedia.active.is_(True)
            )
        )
        if active_count >= MAX_PRODUCT_MEDIA_PER_OWNER:
            raise CatalogConflict("active Product Media limit was reached")
    changed: list[str] = []
    previous_active = media.active
    for field, value in (
        ("asset_url", asset_url),
        ("alt_text", alt_text),
        ("display_order", display_order),
        ("active", active),
    ):
        if value is not UNSET and getattr(media, field) != value:
            setattr(media, field, value)
            changed.append(field)
    if not media.active and media.is_primary:
        media.is_primary = False
        changed.append("is_primary")
    event_time = now or _utcnow()
    media.updated_at = event_time
    await session.flush()
    if previous_active and not media.active:
        action = "commerce.product_media.deactivated"
    elif not previous_active and media.active:
        action = "commerce.product_media.reactivated"
    else:
        action = "commerce.product_media.updated"
    await _append_media_audit(
        session,
        actor=actor,
        administrator=administrator,
        media=media,
        action=action,
        metadata={
            "changed_fields": changed,
            "previous_active": previous_active,
            "new_active": media.active,
        },
        occurred_at=event_time,
    )
    return media


async def set_primary_media(
    session: AsyncSession,
    *,
    media_id: uuid.UUID,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> ProductMedia:
    actor = await require_commerce_administrator(session, administrator)
    existing = await session.get(ProductMedia, media_id)
    if existing is None:
        raise CatalogNotFound("Product Media was not found")
    await _lock_media_owner(
        session,
        product_id=existing.product_id,
        sellable_item_id=existing.sellable_item_id,
    )
    media = await session.scalar(
        select(ProductMedia)
        .where(ProductMedia.media_id == media_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if media is None:
        raise CatalogNotFound("Product Media was not found")
    if not media.active:
        raise CatalogConflict("inactive Product Media cannot be primary")
    owner_filter = _media_owner_filter(
        product_id=media.product_id, sellable_item_id=media.sellable_item_id
    )
    event_time = now or _utcnow()
    previous_primary_id = await session.scalar(
        select(ProductMedia.media_id).where(
            owner_filter,
            ProductMedia.active.is_(True),
            ProductMedia.is_primary.is_(True),
        )
    )
    await session.execute(
        update(ProductMedia)
        .where(
            owner_filter,
            ProductMedia.active.is_(True),
            ProductMedia.is_primary.is_(True),
            ProductMedia.media_id != media.media_id,
        )
        .values(is_primary=False, updated_at=event_time)
    )
    media.is_primary = True
    media.updated_at = event_time
    await session.flush()
    await _append_media_audit(
        session,
        actor=actor,
        administrator=administrator,
        media=media,
        action="commerce.product_media.primary_changed",
        metadata={
            "owner_scope": (
                "product" if media.product_id is not None else "sellable_item"
            ),
            "previous_primary_media_id": (
                None if previous_primary_id is None else str(previous_primary_id)
            ),
        },
        occurred_at=event_time,
    )
    return media


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
