from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.modules.product_offer.service import (
    ProductOfferCdfQuoteUnavailable,
    ProductOfferRow,
    _clamp_limit,
    _compose_offer,
    _normalize_query,
    _validate_budget,
)
from app.schemas.product_offer import ProductOfferResponse


def _product(*, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        product_id=uuid.uuid4(),
        name="Fictional Air Fryer",
        category_code="air_fryer",
        description="Fictional product used only for isolated tests.",
        active=active,
    )


def _item(*, active: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        sellable_item_id=uuid.uuid4(),
        sku="FICTIONAL-8L",
        model_label="Model 8L",
        attributes={"capacity_l": 8},
        active=active,
    )


def _price() -> SimpleNamespace:
    return SimpleNamespace(
        price_id=uuid.uuid4(),
        amount=Decimal("60.00"),
        currency="USD",
        effective_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


def _inventory(status: str) -> SimpleNamespace:
    return SimpleNamespace(
        inventory_id=uuid.uuid4(),
        status=status,
        updated_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


def _rate() -> SimpleNamespace:
    return SimpleNamespace(
        exchange_rate_id=uuid.uuid4(),
        rate=Decimal("2800.000000"),
        effective_at=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )


def _media(*, suffix: str) -> SimpleNamespace:
    return SimpleNamespace(
        media_id=uuid.uuid4(),
        asset_url=f"https://example.invalid/product/{suffix}.jpg",
        alt_text=f"Fictional {suffix} image",
    )


def _offer(
    *,
    product=None,
    item=None,
    price=None,
    inventory=None,
    rate=None,
) -> ProductOfferResponse:
    return _compose_offer(
        ProductOfferRow(
            product or _product(),
            item or _item(),
            price,
            inventory,
            rate,
        ),
        read_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc),
    )


def test_offer_composes_one_sellable_item_with_parent_product_and_cdf_quote() -> None:
    offer = _offer(price=_price(), inventory=_inventory("available"), rate=_rate())

    assert offer.product_name == "Fictional Air Fryer"
    assert offer.model_label == "Model 8L"
    assert offer.current_usd_price == Decimal("60.00")
    assert offer.inventory_status == "available"
    assert offer.offer_status == "sellable_now"
    assert offer.is_sellable_now is True
    assert offer.derived_cdf_quote is not None
    assert offer.derived_cdf_quote.cdf_amount == Decimal("168000.00")
    assert offer.cdf_quote_status == "available"
    assert offer.cdf_quote_unavailable_reason is None


@pytest.mark.parametrize(
    ("product", "item", "price", "inventory", "status", "reason"),
    [
        (_product(active=False), _item(), _price(), _inventory("available"), "inactive", "product_inactive"),
        (_product(), _item(active=False), _price(), _inventory("available"), "inactive", "sellable_item_inactive"),
        (_product(), _item(), None, _inventory("available"), "price_unavailable", "price_unavailable"),
        (_product(), _item(), _price(), _inventory("out_of_stock"), "out_of_stock", "inventory_out_of_stock"),
        (_product(), _item(), _price(), _inventory("unknown"), "availability_unconfirmed", "availability_unconfirmed"),
        (_product(), _item(), _price(), None, "availability_unconfirmed", "availability_unconfirmed"),
    ],
)
def test_offer_preserves_materially_different_commercial_states(
    product,
    item,
    price,
    inventory,
    status: str,
    reason: str,
) -> None:
    offer = _offer(product=product, item=item, price=price, inventory=inventory)
    offer_with_media = _compose_offer(
        ProductOfferRow(
            product,
            item,
            price,
            inventory,
            None,
            None,
            _media(suffix="status-proof"),
        ),
        read_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert offer.offer_status == status
    assert offer.reason_code == reason
    assert offer.is_sellable_now is False
    assert offer_with_media.offer_status == offer.offer_status
    assert offer_with_media.reason_code == offer.reason_code
    assert offer_with_media.is_sellable_now == offer.is_sellable_now
    assert offer_with_media.primary_media is not None


def test_missing_fx_rate_does_not_create_cdf_price_authority() -> None:
    offer = _offer(price=_price(), inventory=_inventory("available"), rate=None)

    assert offer.current_usd_price == Decimal("60.00")
    assert offer.cdf_quote_status == "cdf_quote_unavailable"
    assert offer.cdf_quote_unavailable_reason == "current_fx_unavailable"
    assert offer.derived_cdf_quote is None


def test_missing_usd_price_marks_cdf_quote_unavailable() -> None:
    offer = _offer(price=None, inventory=_inventory("available"), rate=_rate())

    assert offer.cdf_quote_status == "cdf_quote_unavailable"
    assert offer.cdf_quote_unavailable_reason == "current_usd_price_unavailable"
    assert offer.derived_cdf_quote is None


def test_product_offer_contract_rejects_unknown_fields() -> None:
    valid = _offer(price=_price(), inventory=_inventory("available")).model_dump()
    valid["margin"] = "hidden"

    with pytest.raises(ValidationError):
        ProductOfferResponse.model_validate(valid)


def test_sellable_item_primary_media_overrides_product_media() -> None:
    item_media = _media(suffix="model8l")
    product_media = _media(suffix="product")
    offer = _compose_offer(
        ProductOfferRow(
            _product(),
            _item(),
            _price(),
            _inventory("available"),
            _rate(),
            item_media,
            product_media,
        ),
        read_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert offer.primary_media is not None
    assert offer.primary_media.media_id == item_media.media_id
    assert offer.primary_media.source_scope == "sellable_item"
    assert offer.offer_status == "sellable_now"


def test_product_primary_media_is_fallback_and_does_not_change_status() -> None:
    product_media = _media(suffix="product")
    without_media = _offer(price=_price(), inventory=_inventory("out_of_stock"))
    with_media = _compose_offer(
        ProductOfferRow(
            _product(),
            _item(),
            _price(),
            _inventory("out_of_stock"),
            None,
            None,
            product_media,
        ),
        read_at=datetime(2026, 8, 13, 1, 2, 3, tzinfo=timezone.utc),
    )

    assert without_media.primary_media is None
    assert with_media.primary_media is not None
    assert with_media.primary_media.source_scope == "product"
    assert with_media.offer_status == without_media.offer_status == "out_of_stock"
    assert with_media.is_sellable_now is without_media.is_sellable_now is False


def test_search_input_helpers_preserve_bounded_deterministic_filters() -> None:
    assert _normalize_query(" air fryer ") == "air fryer"
    assert _normalize_query("  ") is None
    assert _clamp_limit(500) == 100
    assert _validate_budget(Decimal("160000.00"), field="max_budget_cdf") == Decimal(
        "160000.00"
    )


@pytest.mark.parametrize("budget", [Decimal("0"), Decimal("-1"), Decimal("NaN")])
def test_budget_filters_reject_non_positive_or_non_finite_values(budget: Decimal) -> None:
    with pytest.raises(ValueError):
        _validate_budget(budget, field="max_budget_usd")


def test_search_query_rejects_unbounded_text() -> None:
    with pytest.raises(ValueError):
        _normalize_query("x" * 121)


def test_cdf_budget_failure_has_stable_typed_code() -> None:
    assert ProductOfferCdfQuoteUnavailable.code == "CDF_QUOTE_UNAVAILABLE"
