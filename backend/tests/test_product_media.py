from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.models.catalog import (
    MAX_MEDIA_ALT_TEXT_LENGTH,
    MAX_MEDIA_ASSET_URL_LENGTH,
    ProductMedia,
    normalize_media_alt_text,
    normalize_media_asset_url,
)
from app.schemas.commerce_admin import ProductMediaCreate, ProductMediaUpdate


@pytest.mark.parametrize(
    "asset_url",
    [
        "http://example.invalid/image.jpg",
        "file:///tmp/image.jpg",
        "data:image/png;base64,AAAA",
        "javascript:alert(1)",
        "https://localhost/image.jpg",
        "https://catalog:secret@example.invalid/image.jpg",
        "https://127.0.0.1/image.jpg",
        "https://10.0.0.1/image.jpg",
        "https://169.254.1.1/image.jpg",
        "https://192.168.1.1/image.jpg",
    ],
)
def test_asset_url_rejects_unsafe_locator_shapes(asset_url: str) -> None:
    with pytest.raises(ValueError):
        normalize_media_asset_url(asset_url)


def test_asset_url_accepts_https_without_network_access() -> None:
    value = "https://example.invalid/product/image.webp"
    assert normalize_media_asset_url(value) == value


def test_asset_url_and_alt_text_are_bounded() -> None:
    with pytest.raises(ValueError):
        normalize_media_asset_url(
            "https://example.invalid/" + "x" * MAX_MEDIA_ASSET_URL_LENGTH
        )
    with pytest.raises(ValueError):
        normalize_media_alt_text("x" * (MAX_MEDIA_ALT_TEXT_LENGTH + 1))
    assert normalize_media_alt_text("  Fictional front view  ") == (
        "Fictional front view"
    )


def test_media_payload_requires_exactly_one_owner_and_rejects_unknown_fields() -> None:
    product_id = uuid.uuid4()
    item_id = uuid.uuid4()
    valid = {
        "product_id": product_id,
        "asset_url": "https://example.invalid/product/image.webp",
    }
    assert ProductMediaCreate.model_validate(valid).product_id == product_id
    assert ProductMediaCreate.model_validate(
        {**valid, "product_id": None, "sellable_item_id": item_id}
    ).sellable_item_id == item_id
    for invalid in (
        {**valid, "sellable_item_id": item_id},
        {"asset_url": valid["asset_url"]},
        {**valid, "storage_bucket": "not-authorized"},
        {**valid, "is_primary": True, "active": False},
    ):
        with pytest.raises(ValidationError):
            ProductMediaCreate.model_validate(invalid)


def test_media_update_bounds_and_nullable_alt_text() -> None:
    assert ProductMediaUpdate(alt_text=None).alt_text is None
    with pytest.raises(ValidationError):
        ProductMediaUpdate(display_order=1000)
    with pytest.raises(ValidationError):
        ProductMediaUpdate(active=None)
    with pytest.raises(ValidationError):
        ProductMediaUpdate(asset_url=None)


def test_product_media_model_applies_url_alt_and_order_validation() -> None:
    media = ProductMedia(
        product_id=uuid.uuid4(),
        sellable_item_id=None,
        asset_url=" https://example.invalid/product/image.webp ",
        alt_text=" Fictional product image ",
        display_order=5,
    )
    assert media.asset_url == "https://example.invalid/product/image.webp"
    assert media.alt_text == "Fictional product image"
    with pytest.raises(ValueError):
        media.display_order = -1
