"""Strict Administrator contracts for authoritative commerce maintenance."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.models.catalog import (
    MAX_MEDIA_ALT_TEXT_LENGTH,
    MAX_MEDIA_ASSET_URL_LENGTH,
    MAX_MEDIA_DISPLAY_ORDER,
    normalize_media_alt_text,
    normalize_media_asset_url,
    normalize_category_code,
    normalize_optional_label,
    normalize_required_text,
    normalize_sku,
    validate_sellable_attributes,
)

AttributeValue = StrictStr | StrictInt | StrictBool
MoneyAmount = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
ExchangeRateAmount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=6)]


class StrictCommerceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ProductCreate(StrictCommerceModel):
    name: str = Field(min_length=1, max_length=200)
    category_code: str = Field(min_length=1, max_length=50)
    description: str = Field(min_length=1, max_length=4000)
    active: bool = True

    @field_validator("name")
    @classmethod
    def _name(cls, value: str) -> str:
        return normalize_required_text(value, field="name", maximum=200)

    @field_validator("category_code")
    @classmethod
    def _category(cls, value: str) -> str:
        return normalize_category_code(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str) -> str:
        return normalize_required_text(value, field="description", maximum=4000)


class ProductUpdate(StrictCommerceModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category_code: str | None = Field(default=None, min_length=1, max_length=50)
    description: str | None = Field(default=None, min_length=1, max_length=4000)
    active: bool | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(value, field="name", maximum=200)

    @field_validator("category_code")
    @classmethod
    def _category(cls, value: str | None) -> str | None:
        return None if value is None else normalize_category_code(value)

    @field_validator("description")
    @classmethod
    def _description(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return normalize_required_text(value, field="description", maximum=4000)

    @model_validator(mode="after")
    def _provided_fields_are_not_null(self) -> "ProductUpdate":
        for field in self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProductResponse(StrictCommerceModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, protected_namespaces=()
    )

    product_id: UUID
    name: str
    category_code: str
    description: str
    active: bool
    created_at: datetime
    updated_at: datetime


class ProductListResponse(StrictCommerceModel):
    items: list[ProductResponse]


class SellableItemCreate(StrictCommerceModel):
    model_label: str | None = Field(default=None, max_length=100)
    sku: str | None = Field(default=None, max_length=64)
    attributes: dict[str, AttributeValue] = Field(default_factory=dict)
    active: bool = True

    @field_validator("model_label")
    @classmethod
    def _model_label(cls, value: str | None) -> str | None:
        return normalize_optional_label(value)

    @field_validator("sku")
    @classmethod
    def _sku(cls, value: str | None) -> str | None:
        return normalize_sku(value)

    @field_validator("attributes", mode="before")
    @classmethod
    def _attributes(cls, value: object) -> dict[str, str | int | bool]:
        return validate_sellable_attributes(value)


class SellableItemUpdate(StrictCommerceModel):
    model_label: str | None = Field(default=None, max_length=100)
    sku: str | None = Field(default=None, max_length=64)
    attributes: dict[str, AttributeValue] | None = None
    active: bool | None = None

    @field_validator("model_label")
    @classmethod
    def _model_label(cls, value: str | None) -> str | None:
        return normalize_optional_label(value)

    @field_validator("sku")
    @classmethod
    def _sku(cls, value: str | None) -> str | None:
        return normalize_sku(value)

    @field_validator("attributes", mode="before")
    @classmethod
    def _attributes(
        cls, value: object
    ) -> dict[str, str | int | bool] | None:
        return None if value is None else validate_sellable_attributes(value)

    @model_validator(mode="after")
    def _required_fields_are_not_null(self) -> "SellableItemUpdate":
        for field in ("attributes", "active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class SellableItemResponse(StrictCommerceModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, protected_namespaces=()
    )

    sellable_item_id: UUID
    product_id: UUID
    model_label: str | None
    sku: str | None
    attributes: dict[str, AttributeValue]
    active: bool
    created_at: datetime
    updated_at: datetime


class SellableItemListResponse(StrictCommerceModel):
    items: list[SellableItemResponse]


class ProductMediaCreate(StrictCommerceModel):
    product_id: UUID | None = None
    sellable_item_id: UUID | None = None
    asset_url: str = Field(min_length=1, max_length=MAX_MEDIA_ASSET_URL_LENGTH)
    alt_text: str | None = Field(default=None, max_length=MAX_MEDIA_ALT_TEXT_LENGTH)
    is_primary: bool = False
    display_order: int = Field(default=0, ge=0, le=MAX_MEDIA_DISPLAY_ORDER)
    active: bool = True

    @field_validator("asset_url")
    @classmethod
    def _asset_url(cls, value: str) -> str:
        return normalize_media_asset_url(value)

    @field_validator("alt_text")
    @classmethod
    def _alt_text(cls, value: str | None) -> str | None:
        return normalize_media_alt_text(value)

    @model_validator(mode="after")
    def _exactly_one_owner(self) -> "ProductMediaCreate":
        if (self.product_id is None) == (self.sellable_item_id is None):
            raise ValueError("exactly one Product Media owner is required")
        if self.is_primary and not self.active:
            raise ValueError("primary media must be active")
        return self


class ProductMediaUpdate(StrictCommerceModel):
    asset_url: str | None = Field(
        default=None, min_length=1, max_length=MAX_MEDIA_ASSET_URL_LENGTH
    )
    alt_text: str | None = Field(default=None, max_length=MAX_MEDIA_ALT_TEXT_LENGTH)
    display_order: int | None = Field(
        default=None, ge=0, le=MAX_MEDIA_DISPLAY_ORDER
    )
    active: bool | None = None

    @field_validator("asset_url")
    @classmethod
    def _asset_url(cls, value: str | None) -> str | None:
        return None if value is None else normalize_media_asset_url(value)

    @field_validator("alt_text")
    @classmethod
    def _alt_text(cls, value: str | None) -> str | None:
        return normalize_media_alt_text(value)

    @model_validator(mode="after")
    def _provided_fields_are_valid(self) -> "ProductMediaUpdate":
        for field in ("asset_url", "display_order", "active"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProductMediaSetPrimary(StrictCommerceModel):
    pass


class ProductMediaResponse(StrictCommerceModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, protected_namespaces=()
    )

    media_id: UUID
    product_id: UUID | None
    sellable_item_id: UUID | None
    asset_url: str
    alt_text: str | None
    is_primary: bool
    display_order: int
    active: bool
    created_at: datetime
    updated_at: datetime


class ProductMediaListResponse(StrictCommerceModel):
    items: list[ProductMediaResponse]


def _reject_float(value: object) -> object:
    if isinstance(value, float):
        raise ValueError("decimal values must be supplied without floating point")
    return value


class CurrentUsdPriceSet(StrictCommerceModel):
    amount: MoneyAmount
    currency: Literal["USD"] = "USD"

    @field_validator("amount", mode="before")
    @classmethod
    def _amount(cls, value: object) -> object:
        return _reject_float(value)


class PriceResponse(StrictCommerceModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, protected_namespaces=()
    )

    price_id: UUID
    sellable_item_id: UUID
    amount: Decimal
    currency: Literal["USD"]
    effective_at: datetime
    ended_at: datetime | None


class PriceHistoryResponse(StrictCommerceModel):
    items: list[PriceResponse]


class ExchangeRateSet(StrictCommerceModel):
    base_currency: Literal["USD"] = "USD"
    quote_currency: Literal["CDF"] = "CDF"
    rate: ExchangeRateAmount

    @field_validator("rate", mode="before")
    @classmethod
    def _rate(cls, value: object) -> object:
        return _reject_float(value)


class ExchangeRateResponse(StrictCommerceModel):
    model_config = ConfigDict(
        extra="forbid", from_attributes=True, protected_namespaces=()
    )

    exchange_rate_id: UUID
    base_currency: Literal["USD"]
    quote_currency: Literal["CDF"]
    rate: Decimal
    effective_at: datetime
    ended_at: datetime | None


class ExchangeRateHistoryResponse(StrictCommerceModel):
    items: list[ExchangeRateResponse]


class InventoryStatusSet(StrictCommerceModel):
    status: Literal["available", "out_of_stock", "unknown"]


class InventoryStatusResponse(StrictCommerceModel):
    sellable_item_id: UUID
    configured: bool
    status: Literal["available", "out_of_stock", "unknown"]
    inventory_id: UUID | None
    updated_at: datetime | None
