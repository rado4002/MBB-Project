"""Role-shaped, bounded stock inspection for the Business workspace."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import BrowserPrincipal, require_capability
from app.api.browser_auth_errors import BrowserAuthError
from app.database import get_db
from app.modules.catalog.service import get_effective_primary_images
from app.modules.inventory.service import availability_from_quantity, search_stock_items
from app.modules.pricing.service import get_current_usd_prices

router = APIRouter(prefix="/business/stock", tags=["business-stock"])
_require_reader = require_capability("product_offer.read")


class StockSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, max_length=120)
    item_id: UUID | None = None
    limit: int = Field(default=50, ge=1, le=50)


class StockPrimaryMedia(BaseModel):
    asset_url: str
    alt_text: str | None


class StockItemPublic(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())
    product_id: UUID
    sellable_item_id: UUID
    product_name: str
    model_label: str | None
    sku: str | None
    product_active: bool
    variant_active: bool
    availability: Literal["available", "out_of_stock", "unknown"]
    primary_media: StockPrimaryMedia | None


class StockItemAdmin(StockItemPublic):
    quantity: int | None
    inventory_updated_at: str | None
    current_usd_price: str | None


class StockSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[StockItemPublic | StockItemAdmin]
    has_more: bool


@router.get("", response_model=StockSearchResponse)
async def search_stock(
    params: Annotated[StockSearchQuery, Query()], response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> StockSearchResponse:
    manager = "commerce.manage" in principal.capabilities
    try:
        rows, has_more = await search_stock_items(
            db, query=params.query, sellable_item_id=params.item_id,
            operational_only=not manager, limit=params.limit,
        )
        prices = await get_current_usd_prices(
            db, [item.sellable_item_id for _, item, _ in rows]
        ) if manager else {}
        images = await get_effective_primary_images(
            db, [(product.product_id, item.sellable_item_id) for product, item, _ in rows]
        )
    except (SQLAlchemyError, OSError) as exc:
        raise BrowserAuthError(
            status_code=503, code="SERVICE_UNAVAILABLE",
            message="Stock is temporarily unavailable.",
        ) from exc
    items: list[StockItemPublic | StockItemAdmin] = []
    for product, item, inventory in rows:
        facts = dict(
            product_id=product.product_id,
            sellable_item_id=item.sellable_item_id,
            product_name=product.name,
            model_label=item.model_label,
            sku=item.sku,
            product_active=product.active,
            variant_active=item.active,
            availability=availability_from_quantity(None if inventory is None else inventory.quantity),
            primary_media=(None if (image := images.get(item.sellable_item_id)) is None else {
                "asset_url": image.asset_url, "alt_text": image.alt_text,
            }),
        )
        if manager:
            items.append(StockItemAdmin(
                **facts,
                quantity=None if inventory is None else inventory.quantity,
                inventory_updated_at=(None if inventory is None else inventory.updated_at.isoformat()),
                current_usd_price=(
                    None if item.sellable_item_id not in prices
                    else str(prices[item.sellable_item_id].amount)
                ),
            ))
        else:
            items.append(StockItemPublic(**facts))
    response.headers["Cache-Control"] = "no-store"
    return StockSearchResponse(items=items, has_more=has_more)
