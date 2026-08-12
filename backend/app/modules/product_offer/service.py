"""Read-only composition of Catalog, Pricing, Inventory, and derived CDF quotes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import NamedTuple

from sqlalchemy import and_, case, desc, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import Product, SellableItem, normalize_category_code
from app.models.inventory import InventoryRecord
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.modules.pricing.service import CDF, USD, calculate_cdf_amount
from app.schemas.product_offer import (
    DerivedCdfQuoteResponse,
    OfferReasonCode,
    OfferStatus,
    ProductOfferResponse,
    ProductOfferSearchMode,
)

MAX_PRODUCT_OFFER_SEARCH_LIMIT = 100
DEFAULT_PRODUCT_OFFER_SEARCH_LIMIT = 20


class ProductOfferNotFound(Exception):
    pass


class ProductOfferCdfQuoteUnavailable(Exception):
    code = "CDF_QUOTE_UNAVAILABLE"


class ProductOfferRow(NamedTuple):
    product: Product
    sellable_item: SellableItem
    price: SellableItemPrice | None
    inventory: InventoryRecord | None
    exchange_rate: ExchangeRate | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _validate_budget(value: Decimal, *, field: str) -> Decimal:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{field} must be a positive Decimal")
    return value


def _normalize_query(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized:
        return None
    if len(normalized) > 120:
        raise ValueError("query must not exceed 120 characters")
    return normalized


def _clamp_limit(limit: int) -> int:
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be a positive integer")
    return min(limit, MAX_PRODUCT_OFFER_SEARCH_LIMIT)


def _offer_interpretation(
    *,
    product: Product,
    sellable_item: SellableItem,
    price: SellableItemPrice | None,
    inventory: InventoryRecord | None,
) -> tuple[OfferStatus, bool, OfferReasonCode]:
    if not product.active:
        return "inactive", False, "product_inactive"
    if not sellable_item.active:
        return "inactive", False, "sellable_item_inactive"
    if price is None:
        return "price_unavailable", False, "price_unavailable"

    inventory_status = "unknown" if inventory is None else inventory.status
    if inventory_status == "out_of_stock":
        return "out_of_stock", False, "inventory_out_of_stock"
    if inventory_status == "unknown":
        return "availability_unconfirmed", False, "availability_unconfirmed"
    return "sellable_now", True, "sellable_now"


def _compose_offer(row: ProductOfferRow, *, read_at: datetime) -> ProductOfferResponse:
    product, item, price, inventory, rate = row
    offer_status, is_sellable_now, reason_code = _offer_interpretation(
        product=product,
        sellable_item=item,
        price=price,
        inventory=inventory,
    )

    derived_cdf_quote: DerivedCdfQuoteResponse | None = None
    cdf_quote_status = "cdf_quote_unavailable"
    cdf_quote_unavailable_reason = "current_usd_price_unavailable"
    if price is not None and rate is not None:
        derived_cdf_quote = DerivedCdfQuoteResponse(
            cdf_amount=calculate_cdf_amount(price.amount, rate.rate),
            exchange_rate_id=rate.exchange_rate_id,
            usd_to_cdf_rate=rate.rate,
            exchange_rate_effective_at=rate.effective_at,
        )
        cdf_quote_status = "available"
        cdf_quote_unavailable_reason = None
    elif price is not None:
        cdf_quote_unavailable_reason = "current_fx_unavailable"

    return ProductOfferResponse(
        product_id=product.product_id,
        sellable_item_id=item.sellable_item_id,
        sku=item.sku,
        product_name=product.name,
        category_code=product.category_code,
        description=product.description,
        model_label=item.model_label,
        attributes=item.attributes,
        price_id=None if price is None else price.price_id,
        current_usd_price=None if price is None else price.amount,
        price_effective_at=None if price is None else price.effective_at,
        cdf_quote_status=cdf_quote_status,
        cdf_quote_unavailable_reason=cdf_quote_unavailable_reason,
        derived_cdf_quote=derived_cdf_quote,
        inventory_status="unknown" if inventory is None else inventory.status,
        inventory_configured=inventory is not None,
        inventory_updated_at=None if inventory is None else inventory.updated_at,
        offer_status=offer_status,
        is_sellable_now=is_sellable_now,
        reason_code=reason_code,
        read_at=read_at,
    )


def _current_offer_statement():
    return (
        select(Product, SellableItem, SellableItemPrice, InventoryRecord, ExchangeRate)
        .join(Product, Product.product_id == SellableItem.product_id)
        .outerjoin(
            SellableItemPrice,
            and_(
                SellableItemPrice.sellable_item_id == SellableItem.sellable_item_id,
                SellableItemPrice.currency == USD,
                SellableItemPrice.ended_at.is_(None),
            ),
        )
        .outerjoin(
            InventoryRecord,
            InventoryRecord.sellable_item_id == SellableItem.sellable_item_id,
        )
        .outerjoin(
            ExchangeRate,
            and_(
                ExchangeRate.base_currency == USD,
                ExchangeRate.quote_currency == CDF,
                ExchangeRate.ended_at.is_(None),
            ),
        )
    )


def _status_rank_expression():
    return case(
        (
            or_(Product.active.is_(False), SellableItem.active.is_(False)),
            4,
        ),
        (SellableItemPrice.price_id.is_(None), 3),
        (InventoryRecord.status == "out_of_stock", 2),
        (
            or_(
                InventoryRecord.status.is_(None),
                InventoryRecord.status == "unknown",
            ),
            1,
        ),
        else_=0,
    )


def _current_fx_filter():
    return (
        ExchangeRate.base_currency == USD,
        ExchangeRate.quote_currency == CDF,
        ExchangeRate.ended_at.is_(None),
    )


async def _has_current_usd_cdf_rate(session: AsyncSession) -> bool:
    return (
        await session.scalar(
            select(ExchangeRate.exchange_rate_id).where(*_current_fx_filter()).limit(1)
        )
    ) is not None


async def get_product_offer(
    session: AsyncSession,
    sellable_item_id: uuid.UUID,
    *,
    read_at: datetime | None = None,
) -> ProductOfferResponse | None:
    result = await session.execute(
        _current_offer_statement().where(
            SellableItem.sellable_item_id == sellable_item_id
        )
    )
    row = result.one_or_none()
    if row is None:
        return None
    return _compose_offer(ProductOfferRow(*row), read_at=read_at or _utcnow())


async def require_product_offer(
    session: AsyncSession,
    sellable_item_id: uuid.UUID,
    *,
    read_at: datetime | None = None,
) -> ProductOfferResponse:
    offer = await get_product_offer(session, sellable_item_id, read_at=read_at)
    if offer is None:
        raise ProductOfferNotFound("sellable item was not found")
    return offer


async def search_product_offers(
    session: AsyncSession,
    *,
    query: str | None = None,
    category_code: str | None = None,
    max_budget_usd: Decimal | None = None,
    max_budget_cdf: Decimal | None = None,
    search_mode: ProductOfferSearchMode = "include_unavailable",
    limit: int = DEFAULT_PRODUCT_OFFER_SEARCH_LIMIT,
    read_at: datetime | None = None,
) -> list[ProductOfferResponse]:
    normalized_query = _normalize_query(query)
    normalized_category = (
        None if category_code is None else normalize_category_code(category_code)
    )
    safe_limit = _clamp_limit(limit)

    statement = _current_offer_statement()
    if search_mode == "sellable_only":
        statement = statement.where(
            Product.active.is_(True),
            SellableItem.active.is_(True),
            SellableItemPrice.price_id.is_not(None),
            InventoryRecord.status == "available",
        )
    elif search_mode == "include_unavailable":
        statement = statement.where(
            Product.active.is_(True),
            SellableItem.active.is_(True),
        )
    else:
        raise ValueError("unsupported product offer search mode")

    if normalized_query is not None:
        pattern = f"%{normalized_query}%"
        statement = statement.where(
            or_(
                Product.name.ilike(pattern),
                Product.category_code.ilike(pattern),
                Product.description.ilike(pattern),
                SellableItem.model_label.ilike(pattern),
                SellableItem.sku.ilike(pattern),
            )
        )
    if normalized_category is not None:
        statement = statement.where(Product.category_code == normalized_category)
    if max_budget_usd is not None:
        statement = statement.where(
            SellableItemPrice.amount <= _validate_budget(
                max_budget_usd, field="max_budget_usd"
            )
        )
    if max_budget_cdf is not None:
        budget_cdf = _validate_budget(max_budget_cdf, field="max_budget_cdf")
        if not await _has_current_usd_cdf_rate(session):
            raise ProductOfferCdfQuoteUnavailable(
                "current USD to CDF exchange rate is unavailable"
            )
        statement = statement.where(
            func.round(SellableItemPrice.amount * ExchangeRate.rate, 2) <= budget_cdf,
        )

    statement = statement.order_by(
        _status_rank_expression(),
        desc(Product.active),
        desc(SellableItem.active),
        Product.name,
        nulls_last(SellableItem.model_label),
        nulls_last(SellableItem.sku),
        SellableItem.sellable_item_id,
    ).limit(safe_limit)
    rows = await session.execute(statement)
    offer_read_at = read_at or _utcnow()
    return [
        _compose_offer(ProductOfferRow(*row), read_at=offer_read_at)
        for row in rows.all()
    ]
