"""Bounded stock search and server-side Administrator/Operator shaping."""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1 import business_stock
from app.models.catalog import Product, SellableItem
from app.models.inventory import InventoryRecord
from app.models.pricing import SellableItemPrice
from test_commerce_admin_api import (
    ADMIN_PASSWORD, OPERATOR_PASSWORD, ORIGIN, _login,
    harness,  # noqa: F401 -- disposable PostgreSQL/browser-session fixture
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"), reason="Disposable PostgreSQL required",
)


@pytest_asyncio.fixture(loop_scope="function")
async def stock_catalog(harness):  # noqa: F811 -- imported shared fixture
    transport, factory, _administrator_id = harness
    transport.app.include_router(business_stock.router, prefix="/api/v1")
    active_product = Product(
        product_id=uuid.uuid4(), name="Fictional Solar Kit",
        category_code="solar", description="Review fixture", active=True,
    )
    inactive_product = Product(
        product_id=uuid.uuid4(), name="Fictional Reserve Kit",
        category_code="solar", description="Review fixture", active=False,
    )
    variants = [
        SellableItem(sellable_item_id=uuid.uuid4(), product_id=active_product.product_id,
                     model_label="Small panel", sku="SOLAR-SMALL", attributes={}, active=True),
        SellableItem(sellable_item_id=uuid.uuid4(), product_id=active_product.product_id,
                     model_label="Large panel", sku="SOLAR-LARGE", attributes={}, active=True),
        SellableItem(sellable_item_id=uuid.uuid4(), product_id=active_product.product_id,
                     model_label="Inactive panel", sku="SOLAR-INACTIVE", attributes={}, active=False),
        SellableItem(sellable_item_id=uuid.uuid4(), product_id=inactive_product.product_id,
                     model_label="Reserve panel", sku="RESERVE-PANEL", attributes={}, active=True),
    ]
    async with factory() as session:
        session.add_all([active_product, inactive_product, *variants])
        await session.flush()
        session.add_all([
            InventoryRecord(sellable_item_id=variants[0].sellable_item_id, status="available", quantity=5),
            InventoryRecord(sellable_item_id=variants[1].sellable_item_id, status="out_of_stock", quantity=0),
            InventoryRecord(sellable_item_id=variants[2].sellable_item_id, status="available", quantity=9),
        ])
        session.add(SellableItemPrice(
            sellable_item_id=variants[0].sellable_item_id,
            amount=Decimal("45.00"), currency="USD",
        ))
        await session.commit()
    return transport, variants


@pytest.mark.asyncio
async def test_administrator_searches_product_variant_sku_and_exact_item(stock_catalog) -> None:
    transport, variants = stock_catalog
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.admin", ADMIN_PASSWORD)
        base = "/api/v1/business/stock"
        all_items = await client.get(base)
        assert all_items.status_code == 200
        assert all_items.headers["cache-control"] == "no-store"
        assert len(all_items.json()["items"]) == 4
        by_id = {entry["sellable_item_id"]: entry for entry in all_items.json()["items"]}
        assert by_id[str(variants[0].sellable_item_id)]["quantity"] == 5
        assert by_id[str(variants[0].sellable_item_id)]["current_usd_price"] == "45.00"
        assert by_id[str(variants[1].sellable_item_id)]["current_usd_price"] is None
        assert by_id[str(variants[1].sellable_item_id)]["availability"] == "out_of_stock"
        assert by_id[str(variants[2].sellable_item_id)]["variant_active"] is False
        assert by_id[str(variants[3].sellable_item_id)]["product_active"] is False
        for query, count in [
            ("Solar Kit", 3),
            ("Large panel", 1),
            ("SOLAR-SMALL", 1),
            ("Fictional Solar Kit · Large panel", 1),
            ("SKU: SOLAR-SMALL", 1),
        ]:
            response = await client.get(base, params={"query": query})
            assert response.status_code == 200
            assert len(response.json()["items"]) == count
        combined = await client.get(base, params={"query": "Fictional Solar Kit · Large panel"})
        assert combined.json()["items"][0]["sellable_item_id"] == str(variants[1].sellable_item_id)
        sku_label = await client.get(base, params={"query": "SKU: SOLAR-SMALL"})
        assert sku_label.json()["items"][0]["sellable_item_id"] == str(variants[0].sellable_item_id)
        exact = await client.get(base, params={"item_id": str(variants[2].sellable_item_id)})
        assert [entry["sellable_item_id"] for entry in exact.json()["items"]] == [str(variants[2].sellable_item_id)]
        limited = await client.get(base, params={"limit": 1})
        assert len(limited.json()["items"]) == 1 and limited.json()["has_more"] is True
        for invalid in [{"limit": 51}, {"query": "x" * 121}, {"item_id": "invalid"}]:
            assert (await client.get(base, params=invalid)).status_code == 422


@pytest.mark.asyncio
async def test_operator_sees_only_operational_items_and_no_exact_quantity(stock_catalog) -> None:
    transport, variants = stock_catalog
    base = "/api/v1/business/stock"
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as anonymous:
        assert (await anonymous.get(base)).status_code == 401
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        result = await client.get(base)
        assert result.status_code == 200
        items = result.json()["items"]
        assert {entry["sellable_item_id"] for entry in items} == {
            str(variants[0].sellable_item_id), str(variants[1].sellable_item_id),
        }
        assert {entry["availability"] for entry in items} == {"available", "out_of_stock"}
        assert all("quantity" not in entry and "inventory_updated_at" not in entry
                   and "current_usd_price" not in entry for entry in items)
        for hidden in variants[2:]:
            exact = await client.get(base, params={"item_id": str(hidden.sellable_item_id)})
            assert exact.status_code == 200 and exact.json()["items"] == []


@pytest.mark.asyncio
async def test_stock_search_database_failure_is_not_empty_success(stock_catalog, monkeypatch) -> None:
    transport, _variants = stock_catalog

    async def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("private storage detail")

    monkeypatch.setattr(business_stock, "search_stock_items", unavailable)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        response = await client.get("/api/v1/business/stock")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "private storage detail" not in response.text
