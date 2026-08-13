from __future__ import annotations

import inspect
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityErrorCategory,
    CapabilityExecutor,
    CapabilityFailure,
    CapabilitySuccess,
    GetProductDetailsOutput,
    SearchProductsOutput,
    TrustedCapabilityContext,
)
from app.modules.product_offer.service import (
    ProductOfferCdfQuoteUnavailable,
    ProductOfferNotFound,
)
from app.schemas.product_offer import ProductOfferResponse


def _context() -> TrustedCapabilityContext:
    return TrustedCapabilityContext(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        expected_ownership_version=4,
    )


def _offer(
    *,
    status: str = "sellable_now",
    inventory_status: str = "available",
    media_scope: str | None = "product",
) -> ProductOfferResponse:
    now = datetime.now(timezone.utc)
    media = None
    if media_scope is not None:
        media = {
            "media_id": uuid.uuid4(),
            "asset_url": "https://example.invalid/products/fryer.jpg",
            "alt_text": "Fictional air fryer viewed from the front",
            "source_scope": media_scope,
        }
    has_price = status != "price_unavailable"
    has_fx = has_price
    return ProductOfferResponse.model_validate(
        {
            "product_id": uuid.uuid4(),
            "sellable_item_id": uuid.uuid4(),
            "sku": "FICTIONAL-FRYER-8L",
            "product_name": "Fictional Air Fryer",
            "category_code": "air_fryer",
            "description": "A fictional product used only for capability tests.",
            "model_label": "8L",
            "attributes": {"capacity_l": 8},
            "primary_media": media,
            "price_id": uuid.uuid4() if has_price else None,
            "current_usd_price": Decimal("70.00") if has_price else None,
            "price_currency": "USD",
            "price_effective_at": now if has_price else None,
            "cdf_quote_status": "available" if has_fx else "cdf_quote_unavailable",
            "cdf_quote_unavailable_reason": (
                None if has_fx else "current_usd_price_unavailable"
            ),
            "derived_cdf_quote": (
                {
                    "currency": "CDF",
                    "cdf_amount": Decimal("196000.00"),
                    "exchange_rate_id": uuid.uuid4(),
                    "usd_to_cdf_rate": Decimal("2800.000000"),
                    "exchange_rate_effective_at": now,
                }
                if has_fx
                else None
            ),
            "inventory_status": inventory_status,
            "inventory_configured": inventory_status != "unknown",
            "inventory_updated_at": now if inventory_status != "unknown" else None,
            "offer_status": status,
            "is_sellable_now": status == "sellable_now",
            "reason_code": {
                "sellable_now": "sellable_now",
                "out_of_stock": "inventory_out_of_stock",
                "availability_unconfirmed": "availability_unconfirmed",
                "price_unavailable": "price_unavailable",
                "inactive": "sellable_item_inactive",
            }[status],
            "read_at": now,
        }
    )


@asynccontextmanager
async def _session_context(session: object):
    yield session


def _factory(session: object):
    return lambda: _session_context(session)


@pytest.mark.asyncio
async def test_search_products_maps_validated_intent_and_minimizes_results():
    seen = {}
    offer = _offer(media_scope="sellable_item")

    async def search(_session, **kwargs):
        seen.update(kwargs)
        return [offer]

    with (
        patch("app.database.async_session_factory", _factory(object())),
        patch("app.modules.product_offer.service.search_product_offers", search),
    ):
        result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
            requested_name="search_products",
            model_arguments={
                "query": "  air fryer  ",
                "category_code": "AIR_FRYER",
                "max_budget": "200000.00",
                "budget_currency": "CDF",
                "search_mode": "INCLUDE_UNAVAILABLE",
                "limit": 7,
            },
            allowed_capabilities={"search_products"},
            context=_context(),
        )

    assert isinstance(result, CapabilitySuccess)
    assert isinstance(result.output, SearchProductsOutput)
    assert seen == {
        "query": "air fryer",
        "category_code": "air_fryer",
        "search_mode": "include_unavailable",
        "limit": 7,
        "max_budget_usd": None,
        "max_budget_cdf": Decimal("200000.00"),
    }
    item = result.output.items[0]
    assert item.offer_status == "sellable_now"
    assert item.primary_media is not None
    assert item.primary_media.media_id == offer.primary_media.media_id
    assert item.primary_media.source_scope == "sellable_item"
    serialized = result.output.model_dump(mode="json")
    assert "asset_url" not in str(serialized)
    assert "description" not in serialized["items"][0]
    assert "sku" not in serialized["items"][0]
    assert "exchange_rate_id" not in str(serialized)


@pytest.mark.asyncio
async def test_search_products_defaults_to_safe_bounded_service_arguments():
    seen = {}

    async def search(_session, **kwargs):
        seen.update(kwargs)
        return []

    with (
        patch("app.database.async_session_factory", _factory(object())),
        patch("app.modules.product_offer.service.search_product_offers", search),
    ):
        result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
            requested_name="search_products",
            model_arguments={},
            allowed_capabilities={"search_products"},
            context=_context(),
        )

    assert isinstance(result, CapabilitySuccess)
    assert seen["search_mode"] == "sellable_only"
    assert seen["limit"] == 5
    assert seen["max_budget_usd"] is None
    assert seen["max_budget_cdf"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    (
        {"max_budget": Decimal("0"), "budget_currency": "USD"},
        {"max_budget": Decimal("-1"), "budget_currency": "CDF"},
        {"max_budget": Decimal("10"), "budget_currency": "EUR"},
        {"limit": 0},
        {"limit": 11},
        {"limit": "5"},
        {"search_mode": "INCLUDE_INACTIVE"},
        {"query": " "},
        {"sql": "SELECT * FROM mbb.products"},
        {"url": "https://attacker.invalid"},
        {"exchange_rate": "9999"},
        {"offer_status": "sellable_now"},
        {"fields": ["supplier_cost"]},
    ),
)
async def test_search_products_rejects_unbounded_or_unauthorized_arguments(arguments):
    result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
        requested_name="search_products",
        model_arguments=arguments,
        allowed_capabilities={"search_products"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.invalid_arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "trusted_field",
    ("conversation_id", "turn_id", "expected_ownership_version"),
)
async def test_product_capabilities_reject_model_supplied_trusted_context(trusted_field):
    result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
        requested_name="search_products",
        model_arguments={trusted_field: "model-controlled"},
        allowed_capabilities={"search_products"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.invalid_arguments)


@pytest.mark.asyncio
async def test_cdf_search_failure_maps_to_stable_safe_error():
    async def search(_session, **_kwargs):
        raise ProductOfferCdfQuoteUnavailable("missing current FX")

    with (
        patch("app.database.async_session_factory", _factory(object())),
        patch("app.modules.product_offer.service.search_product_offers", search),
    ):
        result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
            requested_name="search_products",
            model_arguments={
                "max_budget": Decimal("100000.00"),
                "budget_currency": "CDF",
            },
            allowed_capabilities={"search_products"},
            context=_context(),
        )

    assert result == CapabilityFailure(
        CapabilityErrorCategory.execution_failed,
        safe_code="cdf_quote_unavailable",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "inventory_status"),
    (
        ("sellable_now", "available"),
        ("out_of_stock", "out_of_stock"),
        ("availability_unconfirmed", "unknown"),
        ("price_unavailable", "available"),
        ("inactive", "available"),
    ),
)
async def test_get_product_details_preserves_truthful_offer_states(
    status,
    inventory_status,
):
    offer = _offer(status=status, inventory_status=inventory_status, media_scope=None)

    async def require(_session, sellable_item_id):
        assert sellable_item_id == offer.sellable_item_id
        return offer

    with (
        patch("app.database.async_session_factory", _factory(object())),
        patch("app.modules.product_offer.service.require_product_offer", require),
    ):
        result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
            requested_name="get_product_details",
            model_arguments={"sellable_item_id": str(offer.sellable_item_id)},
            allowed_capabilities={"get_product_details"},
            context=_context(),
        )

    assert isinstance(result, CapabilitySuccess)
    assert isinstance(result.output, GetProductDetailsOutput)
    assert result.output.product.offer_status == status
    assert result.output.product.availability == inventory_status
    assert result.output.product.description == offer.description
    assert result.output.product.sku == offer.sku
    assert result.output.product.primary_media is None


@pytest.mark.asyncio
async def test_get_product_details_rejects_invalid_uuid_and_maps_not_found():
    invalid = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
        requested_name="get_product_details",
        model_arguments={"sellable_item_id": "not-a-uuid"},
        allowed_capabilities={"get_product_details"},
        context=_context(),
    )

    async def require(_session, _sellable_item_id):
        raise ProductOfferNotFound("missing")

    with (
        patch("app.database.async_session_factory", _factory(object())),
        patch("app.modules.product_offer.service.require_product_offer", require),
    ):
        missing = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
            requested_name="get_product_details",
            model_arguments={"sellable_item_id": str(uuid.uuid4())},
            allowed_capabilities={"get_product_details"},
            context=_context(),
        )

    assert invalid == CapabilityFailure(CapabilityErrorCategory.invalid_arguments)
    assert missing == CapabilityFailure(
        CapabilityErrorCategory.execution_failed,
        safe_code="sellable_item_not_found",
    )


@pytest.mark.asyncio
async def test_registration_does_not_grant_product_capability_exposure():
    result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
        requested_name="search_products",
        model_arguments={},
        allowed_capabilities={"request_human_handoff"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.tool_not_allowed)
    assert AI_CAPABILITY_REGISTRY.specifications({"request_human_handoff"})[0].name == (
        "request_human_handoff"
    )


def test_product_capability_handlers_have_no_write_or_external_io_path():
    import app.ai.capabilities as capability_module

    source = inspect.getsource(capability_module._search_products) + inspect.getsource(
        capability_module._get_product_details
    )
    for prohibited_term in (
        ".add(",
        ".commit(",
        ".delete(",
        ".flush(",
        "create_product",
        "set_current_usd_price",
        "set_current_exchange_rate",
        "set_inventory_status",
        "create_product_media",
        "httpx",
        "requests",
        "fetch_image",
        "send_product_image",
        "get_ai_adapter",
    ):
        assert prohibited_term not in source
