"""Real PostgreSQL/browser-session evidence for the shared Products workspace."""

import os
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.api.v1 import business_products
from app.models.catalog import Product, ProductMedia, SellableItem
from app.models.pricing import SellableItemPrice
from app.models.operator_account import OperatorAccount
from sqlalchemy.exc import SQLAlchemyError
from app.modules.product_offer.service import get_product_offer
from app.operator_identity.browser_auth import SESSION_COOKIE_NAME
from test_commerce_admin_api import (
    ADMIN_PASSWORD, OPERATOR_PASSWORD, ORIGIN, _headers, _login,
    harness,  # noqa: F401 -- shared disposable DB/session fixture
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"), reason="Disposable PostgreSQL required",
)


@pytest_asyncio.fixture(loop_scope="function")
async def workspace(harness):  # noqa: F811 -- imported shared pytest fixture
    transport, factory, _admin = harness
    transport.app.include_router(business_products.router, prefix="/api/v1")
    async with factory() as db:
        products = [Product(
            product_id=uuid.uuid4(), name=name, category_code="review",
            description="Fictional fixture", active=active,
        ) for name, active in [
            ("Z Operational", True), ("A Inactive", False),
            ("B Empty setup", True), ("C Inactive variants", True),
        ]]
        db.add_all(products)
        await db.flush()
        variants = [SellableItem(
            sellable_item_id=uuid.uuid4(), product_id=products[index].product_id,
            model_label=label, active=active, attributes={"capacity": 6},
        ) for index, label, active in [
            (0, "A Inactive model", False), (0, "B Active model", True),
            (0, "C Active model", True), (1, "Active hidden family", True),
            (3, "Inactive setup", False),
        ]]
        db.add_all(variants)
        await db.flush()
        db.add_all([
            ProductMedia(product_id=products[0].product_id, asset_url="https://example.invalid/product.png", active=True, is_primary=True),
            ProductMedia(sellable_item_id=variants[1].sellable_item_id, asset_url="https://example.invalid/variant.png", active=True, is_primary=True),
        ])
        await db.commit()
    yield transport, factory, products, variants


async def snapshot(factory):
    async with factory() as db:
        return [await db.scalar(text(
            f"SELECT COALESCE(jsonb_agg(to_jsonb(t) ORDER BY to_jsonb(t)::text), '[]') "
            f"FROM mbb.{table} t"
        )) for table in ["products", "sellable_items", "sellable_item_prices", "inventory_statuses", "product_media"]]


@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_scoped_visibility_before_limits_and_no_mutations(workspace, role):
    transport, factory, products, variants = workspace
    before = await snapshot(factory)
    password = OPERATOR_PASSWORD if role == "operator" else ADMIN_PASSWORD
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, f"commerce.{ 'operator' if role == 'operator' else 'admin' }", password)
        response = await client.get("/api/v1/business/products?limit=1")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["items"][0]["name"] == ("Z Operational" if role == "operator" else "A Inactive")
        assert response.json()["has_more"] == (role == "administrator")
        if role == "operator":
            assert response.json()["items"][0]["primary_media"]["asset_url"] == "https://example.invalid/product.png"
        detail = await client.get(f"/api/v1/business/products/{products[0].product_id}?limit=1")
        data = detail.json()
        assert detail.status_code == 200 and data["has_more_variants"]
        assert data["primary_media"]["source_scope"] == "product"
        assert data["variants"][0]["model_label"] == ("B Active model" if role == "operator" else "A Inactive model")
        for product in products[1:]:
            response = await client.get(f"/api/v1/business/products/{product.product_id}")
            assert response.status_code == (404 if role == "operator" else 200)
        offers = await client.get(f"/api/v1/operator/product-offers/{variants[1].sellable_item_id}")
        async with factory() as db:
            expected = await get_product_offer(db, variants[1].sellable_item_id)
        actual = offers.json()
        assert actual["offer_status"] == expected.offer_status == "price_unavailable"
        assert actual["current_usd_price"] is None and actual["inventory_status"] == "unknown"
        assert actual["primary_media"]["source_scope"] == "sellable_item"
    assert await snapshot(factory) == before


async def test_read_authorization_strict_inputs_and_maintenance_denial(workspace):
    transport, factory, products, variants = workspace
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        for path in ["/api/v1/business/products", f"/api/v1/business/products/{products[0].product_id}"]:
            assert (await client.get(path)).status_code == 401
        csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        before = await snapshot(factory)
        for query in ["limit=0", "limit=201", "limit=1.5", "active=false", "query=" + "x" * 121]:
            assert (await client.get("/api/v1/business/products?" + query)).status_code == 422
        assert (await client.get(f"/api/v1/business/products/{products[0].product_id}?active=false")).status_code == 422
        assert (await client.get("/api/v1/business/products/not-a-uuid")).status_code == 422
        assert (await client.get("/api/v1/business/products?query=Empty")).json()["items"] == []
        assert (await client.get("/api/v1/operator/commerce/products")).status_code == 403
        for suffix, body in [("price", {"amount": "20.00"}), ("inventory", {"status": "available"})]:
            response = await client.put(
                f"/api/v1/operator/commerce/sellable-items/{variants[1].sellable_item_id}/{suffix}",
                headers=_headers(csrf), json=body,
            )
            assert response.status_code == 403
        assert await snapshot(factory) == before


@pytest.mark.parametrize("state", ["disabled", "analyst", "password_change", "invalid_session"])
async def test_browse_fails_closed_for_invalid_account_or_session(workspace, state):
    transport, factory, products, _variants = workspace
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        if state == "invalid_session":
            client.cookies.clear()
            client.cookies.set(SESSION_COOKIE_NAME, "invalid")
        else:
            async with factory() as db:
                account = await db.scalar(select(OperatorAccount).where(OperatorAccount.username_normalized == "commerce.operator"))
                if state == "disabled":
                    account.status = "disabled"
                elif state == "analyst":
                    account.role = "analyst"
                else:
                    account.must_change_password = True
                    account.temporary_password_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
                await db.commit()
        for path in ["/api/v1/business/products", f"/api/v1/business/products/{products[0].product_id}"]:
            assert (await client.get(path)).status_code in (401, 403)


async def test_browse_database_failure_is_not_an_empty_success(workspace, monkeypatch):
    transport, _factory, _products, _variants = workspace

    async def unavailable(*_args, **_kwargs):
        raise SQLAlchemyError("private storage details")

    monkeypatch.setattr(business_products.service, "list_products", unavailable)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        result = await client.get("/api/v1/business/products")
        assert result.status_code == 503
        assert result.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "private storage details" not in result.text


async def test_separate_commands_preserve_history_and_reread_authoritative_offer(workspace):
    transport, factory, products, variants = workspace
    item_id = variants[1].sellable_item_id
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        base = f"/api/v1/operator/commerce/sellable-items/{item_id}"
        initial = await snapshot(factory)
        for amount in ["55.00", "60.25"]:
            assert (await client.put(base + "/price", headers=_headers(csrf), json={"amount": amount})).status_code == 200
        async with factory() as db:
            prices = (await db.scalars(select(SellableItemPrice).where(SellableItemPrice.sellable_item_id == item_id))).all()
            assert len(prices) == 2
            assert {p.amount for p in prices} == {Decimal("55.00"), Decimal("60.25")}
            assert sum(p.ended_at is None for p in prices) == 1
            assert next(p for p in prices if p.amount == Decimal("55.00")).ended_at is not None
        priced = await snapshot(factory)
        assert priced[:2] == initial[:2] and priced[3] == initial[3]
        assert priced[4] == initial[4]
        for status, offer_status in [("available", "sellable_now"), ("out_of_stock", "out_of_stock"), ("unknown", "availability_unconfirmed")]:
            assert (await client.put(base + "/inventory", headers=_headers(csrf), json={"status": status})).status_code == 200
            after = await snapshot(factory)
            assert after[:3] == priced[:3]
            assert after[4] == priced[4]
            response = await client.get(f"/api/v1/operator/product-offers/{item_id}")
            assert response.json()["current_usd_price"] == "60.25"
            assert response.json()["inventory_status"] == status
            assert response.json()["offer_status"] == offer_status
            assert response.json()["cdf_quote_status"] == "cdf_quote_unavailable"


async def test_both_commands_keep_csrf_origin_reauthentication_and_validation(workspace, monkeypatch):
    transport, factory, _products, variants = workspace
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        before = await snapshot(factory)
        base = f"/api/v1/operator/commerce/sellable-items/{variants[1].sellable_item_id}"
        for suffix, body in [("price", {"amount": "20.00"}), ("inventory", {"status": "available"})]:
            assert (await client.put(base + "/" + suffix, json=body)).status_code == 403
            assert (await client.put(base + "/" + suffix, headers=_headers(csrf, origin="https://attacker.example"), json=body)).status_code == 403
        for suffix, body in [("price", {"amount": 20.25}), ("price", {"amount": "0.00"}), ("inventory", {"status": "restocking"})]:
            assert (await client.put(base + "/" + suffix, headers=_headers(csrf), json=body)).status_code == 422
        future = time.time() + 1000
        monkeypatch.setattr("app.api.browser_auth_deps.time.time", lambda: future)
        for suffix, body in [("price", {"amount": "20.00"}), ("inventory", {"status": "available"})]:
            result = await client.put(base + "/" + suffix, headers=_headers(csrf), json=body)
            assert result.status_code == 403
            assert result.json()["error"]["code"] == "recent_reauthentication_required"
        assert await snapshot(factory) == before
