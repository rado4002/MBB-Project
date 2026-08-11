from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.models.catalog import MAX_ATTRIBUTE_KEYS, Product, SellableItem
from app.modules.pricing.service import calculate_cdf_amount
from app.schemas.commerce_admin import (
    CurrentUsdPriceSet,
    ExchangeRateSet,
    ProductCreate,
    ProductUpdate,
    SellableItemCreate,
    SellableItemUpdate,
)


def test_catalog_tables_do_not_duplicate_pricing_or_inventory_authority() -> None:
    product_columns = set(Product.__table__.columns.keys())
    item_columns = set(SellableItem.__table__.columns.keys())
    forbidden = {"price", "amount", "currency", "inventory", "status", "quantity"}
    assert product_columns.isdisjoint(forbidden)
    assert item_columns.isdisjoint(forbidden)


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "", "category_code": "air_fryer", "description": "Valid"},
        {"name": "x" * 201, "category_code": "air_fryer", "description": "Valid"},
        {"name": "Valid", "category_code": "Air Fryer", "description": "Valid"},
        {"name": "Valid", "category_code": "air_fryer", "description": ""},
        {
            "name": "Valid",
            "category_code": "air_fryer",
            "description": "Valid",
            "unknown": True,
        },
    ],
)
def test_product_contract_rejects_unbounded_or_unknown_data(payload: dict) -> None:
    with pytest.raises(ValidationError):
        ProductCreate.model_validate(payload)


@pytest.mark.parametrize(
    "attributes",
    [
        {"nested": {"value": 1}},
        {"array": [1, 2]},
        {"decimal_number": 1.5},
        {"Bad Key": 1},
        {"x" * 41: 1},
        {"text": "x" * 201},
        {f"key_{index}": index for index in range(MAX_ATTRIBUTE_KEYS + 1)},
    ],
)
def test_sellable_attributes_are_bounded_flat_primitives(attributes: object) -> None:
    with pytest.raises(ValidationError):
        SellableItemCreate.model_validate({"attributes": attributes})


def test_sellable_contract_normalizes_optional_identity_fields() -> None:
    item = SellableItemCreate.model_validate(
        {
            "model_label": " Model 8L ",
            "sku": " fictional-8l ",
            "attributes": {"capacity_l": 8, "usb_c": True, "finish": " Black "},
        }
    )
    assert item.model_label == "Model 8L"
    assert item.sku == "FICTIONAL-8L"
    assert item.attributes == {"capacity_l": 8, "usb_c": True, "finish": "Black"}


@pytest.mark.parametrize(
    ("schema", "payload"),
    [
        (ProductUpdate, {"name": None}),
        (ProductUpdate, {"active": None}),
        (SellableItemUpdate, {"attributes": None}),
        (SellableItemUpdate, {"active": None}),
    ],
)
def test_update_contracts_reject_null_for_required_state(schema: type, payload: dict) -> None:
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


@pytest.mark.parametrize("amount", ["0", "-1", "1.001", "10000000000.00", 60.0])
def test_usd_price_contract_rejects_invalid_precision_range_and_float(amount: object) -> None:
    with pytest.raises(ValidationError):
        CurrentUsdPriceSet.model_validate({"amount": amount})


@pytest.mark.parametrize("rate", ["0", "-1", "1.0000001", "1000000000000.000000", 2800.0])
def test_exchange_rate_contract_rejects_invalid_precision_range_and_float(rate: object) -> None:
    with pytest.raises(ValidationError):
        ExchangeRateSet.model_validate({"rate": rate})


def test_cdf_quote_uses_decimal_half_up_two_place_rounding() -> None:
    assert calculate_cdf_amount(Decimal("60.00"), Decimal("2800.123456")) == Decimal(
        "168007.41"
    )
    assert calculate_cdf_amount(Decimal("0.01"), Decimal("0.5")) == Decimal("0.01")
