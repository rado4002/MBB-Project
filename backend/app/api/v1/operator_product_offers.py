"""Browser-only, read-only access to authoritative Product Offers."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import BrowserPrincipal, require_capability
from app.api.browser_auth_errors import BrowserAuthError
from app.database import get_db
from app.modules.product_offer import service
from app.schemas.product_offer import ProductOfferResponse, ProductOfferSearchResponse

router = APIRouter(prefix="/operator/product-offers", tags=["operator-product-offers"])
_require_reader = require_capability("product_offer.read")


class OfferSearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, max_length=120)
    limit: int = Field(default=20, ge=1, le=20)


def _unavailable() -> BrowserAuthError:
    return BrowserAuthError(
        status_code=503,
        code="SERVICE_UNAVAILABLE",
        message="Product lookup is temporarily unavailable.",
    )


@router.get("", response_model=ProductOfferSearchResponse)
async def search_offers(
    params: Annotated[OfferSearchQuery, Query()],
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductOfferSearchResponse:
    query = params.query.strip()
    if not query:
        raise BrowserAuthError(
            status_code=422, code="VALIDATION_ERROR", message="Enter a product search."
        )
    try:
        items = await service.search_product_offers(
            db, query=query, limit=params.limit, search_mode="include_unavailable"
        )
    except (SQLAlchemyError, OSError) as exc:
        raise _unavailable() from exc
    response.headers["Cache-Control"] = "no-store"
    return ProductOfferSearchResponse(items=items)


@router.get("/{sellable_item_id}", response_model=ProductOfferResponse)
async def get_offer(
    sellable_item_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_reader)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductOfferResponse:
    try:
        offer = await service.get_product_offer(db, sellable_item_id)
    except (SQLAlchemyError, OSError) as exc:
        raise _unavailable() from exc
    if offer is None:
        raise BrowserAuthError(
            status_code=404,
            code="PRODUCT_OFFER_NOT_FOUND",
            message="The product variant was not found.",
        )
    response.headers["Cache-Control"] = "no-store"
    return offer
