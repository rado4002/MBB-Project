"""Strict read contracts for computed Product Offers."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.schemas.commerce_admin import AttributeValue

ProductOfferSearchMode = Literal["sellable_only", "include_unavailable"]
OfferStatus = Literal[
    "sellable_now",
    "availability_unconfirmed",
    "out_of_stock",
    "price_unavailable",
    "inactive",
]
OfferReasonCode = Literal[
    "sellable_now",
    "availability_unconfirmed",
    "inventory_out_of_stock",
    "price_unavailable",
    "product_inactive",
    "sellable_item_inactive",
]
InventoryAvailabilityStatus = Literal["available", "out_of_stock", "unknown"]
CdfQuoteStatus = Literal["available", "cdf_quote_unavailable"]
CdfQuoteUnavailableReason = Literal[
    "current_usd_price_unavailable",
    "current_fx_unavailable",
]
PrimaryMediaSourceScope = Literal["product", "sellable_item"]


class StrictProductOfferModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class DerivedCdfQuoteResponse(StrictProductOfferModel):
    currency: Literal["CDF"] = "CDF"
    cdf_amount: Decimal
    exchange_rate_id: UUID
    usd_to_cdf_rate: Decimal
    exchange_rate_effective_at: datetime


class ProductOfferPrimaryMediaResponse(StrictProductOfferModel):
    media_id: UUID
    asset_url: str
    alt_text: str | None
    source_scope: PrimaryMediaSourceScope


class ProductOfferResponse(StrictProductOfferModel):
    product_id: UUID
    sellable_item_id: UUID
    sku: str | None

    product_name: str
    category_code: str
    description: str
    model_label: str | None
    attributes: dict[str, AttributeValue]
    primary_media: ProductOfferPrimaryMediaResponse | None

    price_id: UUID | None
    current_usd_price: Decimal | None
    price_currency: Literal["USD"] = "USD"
    price_effective_at: datetime | None
    cdf_quote_status: CdfQuoteStatus
    cdf_quote_unavailable_reason: CdfQuoteUnavailableReason | None
    derived_cdf_quote: DerivedCdfQuoteResponse | None

    inventory_status: InventoryAvailabilityStatus
    inventory_configured: bool
    inventory_updated_at: datetime | None

    offer_status: OfferStatus
    is_sellable_now: bool
    reason_code: OfferReasonCode
    read_at: datetime


class ProductOfferSearchResponse(StrictProductOfferModel):
    items: list[ProductOfferResponse]
