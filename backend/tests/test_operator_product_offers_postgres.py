"""Synthetic browser lookup acceptance against the isolated commerce database."""

import pytest
import httpx
from sqlalchemy import text

from test_commerce_admin_api import (
    ADMIN_PASSWORD, DATABASE_URL, OPERATOR_PASSWORD, ORIGIN,
    _headers, _login, harness,  # noqa: F401 — shared isolated database fixture
)

pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="AI2B_TEST_DATABASE_URL is required")


async def _snapshot(factory):
    tables = (
        "products", "sellable_items", "product_media", "sellable_item_prices",
        "inventory_statuses", "exchange_rates", "exchange_rate_authorities",
        "operator_audit_events", "operator_audit_security_metadata",
    )
    async with factory() as session:
        return {
            table: (await session.execute(text(f"SELECT * FROM mbb.{table} ORDER BY 1"))).all()
            for table in tables
        }


async def test_browser_offer_reads_preserve_all_commerce_records(harness):  # noqa: F811 — shared fixture
    transport, factory, _administrator_id = harness
    async with (
        httpx.AsyncClient(transport=transport, base_url=ORIGIN) as admin,
        httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator,
        httpx.AsyncClient(transport=transport, base_url=ORIGIN) as anonymous,
    ):
        csrf = await _login(admin, "commerce.admin", ADMIN_PASSWORD)
        prefix = "/api/v1/operator/commerce"
        created = await admin.post(f"{prefix}/products", headers=_headers(csrf), json={
            "name": "Fictional C1 Fryer", "category_code": "air_fryer", "description": "Synthetic lookup fixture.",
        })
        assert created.status_code == 201
        product_id = created.json()["product_id"]
        ids = []
        for index, status in enumerate(["available", "out_of_stock", "unknown", None]):
            item = await admin.post(f"{prefix}/products/{product_id}/sellable-items", headers=_headers(csrf), json={
                "model_label": f"{index + 1}L", "sku": f"C1-{index}",
            })
            assert item.status_code == 201
            item_id = item.json()["sellable_item_id"]
            ids.append(item_id)
            if status is not None:
                assert (await admin.put(f"{prefix}/sellable-items/{item_id}/price", headers=_headers(csrf), json={"amount": "55.00"})).status_code == 200
                assert (await admin.put(f"{prefix}/sellable-items/{item_id}/inventory", headers=_headers(csrf), json={"status": status})).status_code == 200
        assert (await admin.post(f"{prefix}/product-media", headers=_headers(csrf), json={
            "product_id": product_id, "asset_url": "https://example.invalid/c1.jpg", "is_primary": True,
        })).status_code == 201
        operator_csrf = await _login(operator, "commerce.operator", OPERATOR_PASSWORD)
        before = await _snapshot(factory)
        path = "/api/v1/operator/product-offers"
        assert (await anonymous.get(path, params={"query": "C1 Fryer"})).status_code == 401
        assert (await anonymous.get(f"{path}/{ids[0]}")).status_code == 401
        for client in [admin, operator]:
            response = await client.get(path, params={"query": "C1 Fryer"})
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            offers = response.json()["items"]
            assert [item["offer_status"] for item in offers] == ["sellable_now", "availability_unconfirmed", "out_of_stock", "price_unavailable"]
            for item in offers:
                detail = await client.get(f'{path}/{item["sellable_item_id"]}')
                assert detail.status_code == 200
                assert detail.json()["offer_status"] == item["offer_status"]
                assert detail.json()["primary_media"]["source_scope"] == "product"
                assert detail.json()["derived_cdf_quote"] is None
                assert detail.json()["cdf_quote_status"] == "cdf_quote_unavailable"
        assert (await operator.get(f"{prefix}/products")).status_code == 403
        assert (await operator.put(f"{prefix}/sellable-items/{ids[0]}/inventory", headers=_headers(operator_csrf), json={"status": "out_of_stock"})).status_code == 403
        assert await _snapshot(factory) == before
