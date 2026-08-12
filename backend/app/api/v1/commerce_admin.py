"""Administrator-only browser API for authoritative commerce maintenance."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    get_browser_settings,
    require_capability,
    require_csrf,
    require_recent_reauthentication,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings
from app.database import get_db
from app.modules.catalog import service as catalog_service
from app.modules.commerce_admin import (
    CommerceAdminContext,
    CommerceAuthorizationDenied,
)
from app.modules.inventory import service as inventory_service
from app.modules.pricing import service as pricing_service
from app.request_ids import normalize_or_generate_request_id
from app.schemas.commerce_admin import (
    CurrentUsdPriceSet,
    ExchangeRateHistoryResponse,
    ExchangeRateResponse,
    ExchangeRateSet,
    InventoryStatusResponse,
    InventoryStatusSet,
    PriceHistoryResponse,
    PriceResponse,
    ProductCreate,
    ProductListResponse,
    ProductMediaCreate,
    ProductMediaListResponse,
    ProductMediaResponse,
    ProductMediaSetPrimary,
    ProductMediaUpdate,
    ProductResponse,
    ProductUpdate,
    SellableItemCreate,
    SellableItemListResponse,
    SellableItemResponse,
    SellableItemUpdate,
)

router = APIRouter(prefix="/operator/commerce", tags=["operator-commerce"])
_MANAGE_CAPABILITY = "commerce.manage"
_require_commerce_manager = require_capability(_MANAGE_CAPABILITY)


def _error(status_code: int, code: str, message: str) -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status_code,
        code=code,
        operator_code=code,
        message=message,
    )


def _map_error(exc: Exception) -> BrowserAuthError:
    if isinstance(
        exc,
        (
            catalog_service.CatalogNotFound,
            pricing_service.PricingNotFound,
            inventory_service.InventoryNotFound,
        ),
    ):
        return _error(404, "COMMERCE_RESOURCE_NOT_FOUND", "The commerce resource was not found.")
    if isinstance(exc, catalog_service.CatalogConflict):
        return _error(409, "COMMERCE_CONFLICT", "The commerce data conflicts with current state.")
    if isinstance(exc, CommerceAuthorizationDenied):
        return _error(403, "COMMERCE_ADMINISTRATOR_REQUIRED", "Administrator authority is required.")
    if isinstance(exc, (ValueError, pricing_service.UnsupportedCurrency)):
        return _error(422, "COMMERCE_VALIDATION_FAILED", "The commerce data is invalid.")
    return _error(503, "SERVICE_UNAVAILABLE", "Commerce maintenance is temporarily unavailable.")


def _administrator(request: Request, principal: BrowserPrincipal) -> CommerceAdminContext:
    return CommerceAdminContext(
        actor_account_id=principal.account.account_id,
        request_id=normalize_or_generate_request_id(
            getattr(request.state, "request_id", None)
            or request.headers.get("X-Request-ID")
        ),
        source_network_fingerprint=principal.session.record.ip_prefix_fingerprint,
        user_agent_fingerprint=principal.session.record.user_agent_fingerprint,
    )


def _write_guard(request: Request, settings: Settings) -> None:
    validate_state_changing_request(request, settings)


async def _commit_or_raise(db: AsyncSession, operation: Any) -> Any:
    try:
        result = await operation
        await db.commit()
        return result
    except (
        SQLAlchemyError,
        OSError,
        ValueError,
        catalog_service.CatalogNotFound,
        catalog_service.CatalogConflict,
        pricing_service.PricingNotFound,
        pricing_service.UnsupportedCurrency,
        inventory_service.InventoryNotFound,
        CommerceAuthorizationDenied,
    ) as exc:
        await db.rollback()
        raise _map_error(exc) from exc


async def _read_or_raise(operation: Any) -> Any:
    try:
        return await operation
    except (SQLAlchemyError, OSError) as exc:
        raise _map_error(exc) from exc


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


@router.get("/products", response_model=ProductListResponse)
async def list_products(
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductListResponse:
    items = await _read_or_raise(catalog_service.list_products(db))
    _no_store(response)
    return ProductListResponse(items=[ProductResponse.model_validate(item) for item in items])


@router.get("/products/{product_id}", response_model=ProductResponse)
async def get_product(
    product_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductResponse:
    product = await _read_or_raise(catalog_service.get_product(db, product_id))
    if product is None:
        raise _map_error(catalog_service.CatalogNotFound())
    _no_store(response)
    return ProductResponse.model_validate(product)


@router.post("/products", response_model=ProductResponse, status_code=status.HTTP_201_CREATED)
async def create_product(
    body: ProductCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductResponse:
    _write_guard(request, settings)
    product = await _commit_or_raise(
        db,
        catalog_service.create_product(
            db,
            **body.model_dump(),
            administrator=_administrator(request, principal),
        ),
    )
    _no_store(response)
    return ProductResponse.model_validate(product)


@router.patch("/products/{product_id}", response_model=ProductResponse)
async def update_product(
    product_id: UUID,
    body: ProductUpdate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductResponse:
    _write_guard(request, settings)
    values = body.model_dump(exclude_unset=True)
    product = await _commit_or_raise(
        db,
        catalog_service.update_product(
            db,
            product_id=product_id,
            administrator=_administrator(request, principal),
            **values,
        ),
    )
    _no_store(response)
    return ProductResponse.model_validate(product)


@router.get("/sellable-items", response_model=SellableItemListResponse)
async def list_sellable_items(
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
    product_id: UUID | None = None,
) -> SellableItemListResponse:
    items = await _read_or_raise(
        catalog_service.list_sellable_items(db, product_id=product_id)
    )
    _no_store(response)
    return SellableItemListResponse(
        items=[SellableItemResponse.model_validate(item) for item in items]
    )


@router.get("/sellable-items/{sellable_item_id}", response_model=SellableItemResponse)
async def get_sellable_item(
    sellable_item_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SellableItemResponse:
    item = await _read_or_raise(
        catalog_service.get_sellable_item(db, sellable_item_id)
    )
    if item is None:
        raise _map_error(catalog_service.CatalogNotFound())
    _no_store(response)
    return SellableItemResponse.model_validate(item)


@router.post(
    "/products/{product_id}/sellable-items",
    response_model=SellableItemResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_sellable_item(
    product_id: UUID,
    body: SellableItemCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SellableItemResponse:
    _write_guard(request, settings)
    item = await _commit_or_raise(
        db,
        catalog_service.create_sellable_item(
            db,
            product_id=product_id,
            **body.model_dump(),
            administrator=_administrator(request, principal),
        ),
    )
    _no_store(response)
    return SellableItemResponse.model_validate(item)


@router.patch("/sellable-items/{sellable_item_id}", response_model=SellableItemResponse)
async def update_sellable_item(
    sellable_item_id: UUID,
    body: SellableItemUpdate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> SellableItemResponse:
    _write_guard(request, settings)
    item = await _commit_or_raise(
        db,
        catalog_service.update_sellable_item(
            db,
            sellable_item_id=sellable_item_id,
            administrator=_administrator(request, principal),
            **body.model_dump(exclude_unset=True),
        ),
    )
    _no_store(response)
    return SellableItemResponse.model_validate(item)


@router.get("/product-media/{media_id}", response_model=ProductMediaResponse)
async def get_product_media(
    media_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductMediaResponse:
    media = await _read_or_raise(catalog_service.get_media(db, media_id))
    if media is None:
        raise _map_error(catalog_service.CatalogNotFound())
    _no_store(response)
    return ProductMediaResponse.model_validate(media)


@router.get(
    "/products/{product_id}/media", response_model=ProductMediaListResponse
)
async def list_product_media(
    product_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductMediaListResponse:
    items = await _read_or_raise(
        catalog_service.list_product_media(db, product_id, active_only=False)
    )
    _no_store(response)
    return ProductMediaListResponse(
        items=[ProductMediaResponse.model_validate(item) for item in items]
    )


@router.get(
    "/sellable-items/{sellable_item_id}/media",
    response_model=ProductMediaListResponse,
)
async def list_sellable_item_media(
    sellable_item_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductMediaListResponse:
    items = await _read_or_raise(
        catalog_service.list_sellable_item_media(
            db, sellable_item_id, active_only=False
        )
    )
    _no_store(response)
    return ProductMediaListResponse(
        items=[ProductMediaResponse.model_validate(item) for item in items]
    )


@router.post(
    "/product-media",
    response_model=ProductMediaResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_product_media(
    body: ProductMediaCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductMediaResponse:
    _write_guard(request, settings)
    media = await _commit_or_raise(
        db,
        catalog_service.create_product_media(
            db,
            **body.model_dump(),
            administrator=_administrator(request, principal),
        ),
    )
    _no_store(response)
    return ProductMediaResponse.model_validate(media)


@router.patch("/product-media/{media_id}", response_model=ProductMediaResponse)
async def update_product_media(
    media_id: UUID,
    body: ProductMediaUpdate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductMediaResponse:
    _write_guard(request, settings)
    media = await _commit_or_raise(
        db,
        catalog_service.update_product_media(
            db,
            media_id=media_id,
            administrator=_administrator(request, principal),
            **body.model_dump(exclude_unset=True),
        ),
    )
    _no_store(response)
    return ProductMediaResponse.model_validate(media)


@router.put(
    "/product-media/{media_id}/primary", response_model=ProductMediaResponse
)
async def set_primary_product_media(
    media_id: UUID,
    _body: ProductMediaSetPrimary,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ProductMediaResponse:
    _write_guard(request, settings)
    media = await _commit_or_raise(
        db,
        catalog_service.set_primary_media(
            db,
            media_id=media_id,
            administrator=_administrator(request, principal),
        ),
    )
    _no_store(response)
    return ProductMediaResponse.model_validate(media)


@router.get("/sellable-items/{sellable_item_id}/prices", response_model=PriceHistoryResponse)
async def get_price_history(
    sellable_item_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PriceHistoryResponse:
    items = await _read_or_raise(
        pricing_service.list_price_history(db, sellable_item_id)
    )
    _no_store(response)
    return PriceHistoryResponse(items=[PriceResponse.model_validate(item) for item in items])


@router.put("/sellable-items/{sellable_item_id}/price", response_model=PriceResponse)
async def set_current_price(
    sellable_item_id: UUID,
    body: CurrentUsdPriceSet,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> PriceResponse:
    _write_guard(request, settings)
    price = await _commit_or_raise(
        db,
        pricing_service.set_current_usd_price(
            db,
            sellable_item_id=sellable_item_id,
            amount=body.amount,
            currency=body.currency,
            administrator=_administrator(request, principal),
        ),
    )
    _no_store(response)
    return PriceResponse.model_validate(price)


@router.get(
    "/sellable-items/{sellable_item_id}/inventory",
    response_model=InventoryStatusResponse,
)
async def get_inventory_status(
    sellable_item_id: UUID,
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InventoryStatusResponse:
    result = await _read_or_raise(
        inventory_service.get_inventory_status(db, sellable_item_id)
    )
    _no_store(response)
    return InventoryStatusResponse.model_validate(result, from_attributes=True)


@router.put(
    "/sellable-items/{sellable_item_id}/inventory",
    response_model=InventoryStatusResponse,
)
async def set_inventory_status(
    sellable_item_id: UUID,
    body: InventoryStatusSet,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> InventoryStatusResponse:
    _write_guard(request, settings)
    await _commit_or_raise(
        db,
        inventory_service.set_inventory_status(
            db,
            sellable_item_id=sellable_item_id,
            status=body.status,
            administrator=_administrator(request, principal),
        ),
    )
    result = await _read_or_raise(
        inventory_service.get_inventory_status(db, sellable_item_id)
    )
    _no_store(response)
    return InventoryStatusResponse.model_validate(result, from_attributes=True)


@router.get("/exchange-rates/usd-cdf", response_model=ExchangeRateHistoryResponse)
async def get_exchange_rate_history(
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExchangeRateHistoryResponse:
    items = await _read_or_raise(pricing_service.list_usd_cdf_rate_history(db))
    _no_store(response)
    return ExchangeRateHistoryResponse(
        items=[ExchangeRateResponse.model_validate(item) for item in items]
    )


@router.put("/exchange-rates/usd-cdf", response_model=ExchangeRateResponse)
async def set_exchange_rate(
    body: ExchangeRateSet,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(_require_commerce_manager)],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> ExchangeRateResponse:
    _write_guard(request, settings)
    rate = await _commit_or_raise(
        db,
        pricing_service.set_current_exchange_rate(
            db,
            **body.model_dump(),
            administrator=_administrator(request, principal),
        ),
    )
    _no_store(response)
    return ExchangeRateResponse.model_validate(rate)
