"""Role-sensitive, read-only Product-family browsing for browser users."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import BrowserPrincipal, require_capability
from app.api.browser_auth_errors import BrowserAuthError
from app.database import get_db
from app.modules.catalog import service
from app.schemas.commerce_admin import ProductResponse, SellableItemResponse
from app.schemas.product_offer import ProductOfferPrimaryMediaResponse

router = APIRouter(prefix="/business/products", tags=["business-products"])
_require_reader = require_capability("product_offer.read")


class BrowseQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str | None = Field(default=None, max_length=120)
    limit: int = Field(default=50, ge=1, le=200)


class VariantQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=50, ge=1, le=200)


class BusinessProduct(ProductResponse):
    primary_media: ProductOfferPrimaryMediaResponse | None


class BusinessProductList(BaseModel):
    items: list[BusinessProduct]
    has_more: bool


class BusinessProductDetail(BusinessProduct):
    variants: list[SellableItemResponse]
    has_more_variants: bool


def _product(product, images) -> BusinessProduct:
    image = images.get(product.product_id)
    return BusinessProduct(
        **ProductResponse.model_validate(product).model_dump(),
        primary_media=None if image is None else ProductOfferPrimaryMediaResponse(
            media_id=image.media_id, asset_url=image.asset_url,
            alt_text=image.alt_text, source_scope="product",
        ),
    )


def _unavailable() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=503, code="SERVICE_UNAVAILABLE",
        message="Products are temporarily unavailable.",
    )


@router.get("", response_model=BusinessProductList)
async def list_products(
    params: Annotated[BrowseQuery, Query()], response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BusinessProductList:
    try:
        products = await service.list_products(
            db, limit=params.limit + 1,
            operational_only="commerce.manage" not in principal.capabilities,
            query=params.query.strip() if params.query else None,
        )
        shown = products[:params.limit]
        images = await service.get_product_primary_images(db, [p.product_id for p in shown])
    except (SQLAlchemyError, OSError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return BusinessProductList(
        items=[_product(p, images) for p in shown], has_more=len(products) > params.limit,
    )


@router.get("/{product_id}", response_model=BusinessProductDetail)
async def get_product(
    product_id: UUID, params: Annotated[VariantQuery, Query()], response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BusinessProductDetail:
    operational_only = "commerce.manage" not in principal.capabilities
    try:
        product = await service.get_product(db, product_id)
        variants = await service.list_sellable_items(
            db, product_id=product_id, limit=params.limit + 1,
            operational_only=operational_only,
        ) if product is not None else []
        if product is None or (operational_only and (not product.active or not variants)):
            raise BrowserAuthError(
                status_code=404, code="PRODUCT_NOT_FOUND",
                message="The product is not available to inspect.",
            )
        images = await service.get_product_primary_images(db, [product_id])
    except (SQLAlchemyError, OSError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return BusinessProductDetail(
        **_product(product, images).model_dump(),
        variants=[SellableItemResponse.model_validate(v) for v in variants[:params.limit]],
        has_more_variants=len(variants) > params.limit,
    )
