"""Browser-session Orders reads against an accepted draft in disposable PostgreSQL."""

import os
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text

from app.api.v1 import business_orders
from app.models.inventory import InventoryRecord
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.pricing import SellableItemPrice
from test_commerce_admin_api import (
    ADMIN_PASSWORD, OPERATOR_PASSWORD, ORIGIN, _login,
    harness,  # noqa: F401 -- disposable DB and real browser-session fixture
)
from test_order_drafts_postgres import _prepare, _reply, _seed

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"), reason="Disposable PostgreSQL required",
)


@pytest_asyncio.fixture(loop_scope="function")
async def accepted_order(harness):  # noqa: F811 -- imported fixture
    transport, factory, _admin = harness
    transport.app.include_router(business_orders.router, prefix="/api/v1")
    async with factory() as db:
        await db.execute(text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE"))
        await db.commit()
    try:
        seeded = await _seed(factory)
        prepared = await _prepare(factory, seeded)
        async with factory() as db:
            code = await db.scalar(select(OrderDraft.confirmation_code).where(OrderDraft.draft_id == prepared.draft_id))
        result, _ = await _reply(factory, seeded, content=f"OUI {code}")
        assert result is not None and result.state == "confirmed" and result.order_id is not None
        yield transport, factory, seeded, result.order_id
    finally:
        async with factory() as db:
            await db.execute(text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE"))
            await db.commit()


@pytest.mark.asyncio
async def test_authoritative_snapshot_survives_current_commercial_changes(accepted_order):
    transport, factory, seeded, order_id = accepted_order
    async with factory() as db:
        original = await db.get(Order, order_id)
        original_items = original.items
        original_total = original.total_amount
        original_updated_at = original.updated_at
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        inventory.quantity = 0
        price = await db.get(SellableItemPrice, seeded.price_id)
        price.ended_at = price.effective_at + timedelta(seconds=1)
        await db.commit()
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        listing = await client.get("/api/v1/business/orders?status=pending&limit=1")
        assert listing.status_code == 200 and listing.headers["cache-control"] == "no-store"
        assert listing.json()["has_more"] is False
        assert [item["order_id"] for item in listing.json()["items"]] == [str(order_id)]
        detail = await client.get(f"/api/v1/business/orders/{order_id}")
        assert detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
        body = detail.json()
        assert body["status"] == "pending" and body["conversation_id"] == str(seeded.conversation_id)
        assert Decimal(body["total_cdf"]) == original_total
        assert body["items"] == original_items
        assert body["items"][0]["price_id"] == str(seeded.price_id)
        assert body["items"][0]["sellable_item_id"] == str(seeded.sellable_item_id)
        assert Decimal(body["items"][0]["usd_to_cdf_rate"]) == Decimal("2800.000000")
        assert (await client.get("/api/v1/business/orders?status=delivered")).json()["items"] == []
        assert (await client.post("/api/v1/business/orders", json={})).status_code == 405
        assert (await client.put(f"/api/v1/business/orders/{order_id}", json={})).status_code == 405
    async with factory() as db:
        stored = await db.get(Order, order_id)
        assert stored.items == original_items and stored.total_amount == original_total
        assert stored.updated_at == original_updated_at and stored.status == "pending"
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert (await db.get(InventoryRecord, seeded.inventory_id)).quantity == 0


@pytest.mark.asyncio
async def test_session_permissions_validation_and_verified_link(accepted_order):
    transport, factory, seeded, order_id = accepted_order
    url = f"/api/v1/business/orders/{order_id}"
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        assert (await client.get("/api/v1/business/orders")).status_code == 401
        assert (await client.get(url)).status_code == 401
        await _login(client, "commerce.admin", ADMIN_PASSWORD)
        assert (await client.get(url)).status_code == 200
        for query in ("status=unknown", "limit=0", "limit=51", "offset=-1", "unused=1"):
            assert (await client.get(f"/api/v1/business/orders?{query}")).status_code == 422
        assert (await client.get("/api/v1/business/orders/not-a-uuid")).status_code == 422
        async with factory() as db:
            draft = await db.scalar(select(OrderDraft).where(OrderDraft.order_id == order_id))
            draft.order_id = None
            await db.commit()
        assert (await client.get(url)).json()["conversation_id"] is None
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        async with factory() as db:
            account = await db.scalar(select(OperatorAccount).where(OperatorAccount.username_normalized == "commerce.operator"))
            account.role = "analyst"
            await db.commit()
        assert (await client.get(url)).status_code == 403
