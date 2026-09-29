"""PostgreSQL and browser API evidence for operational stock commands."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.models.catalog import Product, SellableItem
from app.models.inventory import InventoryRecord
from app.models.operator_audit import OperatorAuditEvent
from app.models.stock_movement import StockMovement
from test_commerce_admin_api import (
    ADMIN_PASSWORD, OPERATOR_PASSWORD, ORIGIN, _headers, _login,
    harness,  # noqa: F401 -- isolated PostgreSQL/browser-session fixture
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"), reason="Disposable PostgreSQL required",
)


@pytest_asyncio.fixture(loop_scope="function")
async def stock(harness):  # noqa: F811 -- imported shared fixture
    transport, factory, administrator_id = harness
    product = Product(
        product_id=uuid.uuid4(), name="Fictional Stock Product",
        category_code="review", description="Stock fixture", active=True,
    )
    item = SellableItem(
        sellable_item_id=uuid.uuid4(), product_id=product.product_id,
        model_label="Fictional variant", sku="STOCK-V2-B1", attributes={}, active=True,
    )
    async with factory() as session:
        session.add_all([product, item])
        await session.commit()
    return transport, factory, administrator_id, item.sellable_item_id


def _path(item_id: uuid.UUID) -> str:
    return f"/api/v1/operator/commerce/sellable-items/{item_id}/inventory"


def _adjust(key: uuid.UUID, quantity: int, before: dict, reason: str = "Verified physical count") -> dict:
    return {
        "operation_key": str(key), "corrected_quantity": quantity,
        "expected_quantity": before["quantity"],
        "expected_updated_at": before["updated_at"], "reason": reason,
    }


def _receive(key: uuid.UUID, amount: int, before: dict) -> dict:
    return {
        "operation_key": str(key), "received_amount": amount,
        "expected_quantity": before["quantity"],
        "expected_updated_at": before["updated_at"],
    }


@pytest.mark.asyncio
async def test_adjust_receive_retry_activity_offer_and_legacy_write(stock) -> None:
    transport, factory, administrator_id, item_id = stock
    path = _path(item_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        headers = _headers(csrf)
        before = (await client.get(path)).json()
        assert before["quantity"] is None and before["updated_at"] is None
        adjust_body = _adjust(uuid.uuid4(), 10, before)
        adjusted = await client.post(path + "/adjust", headers=headers, json=adjust_body)
        assert adjusted.status_code == 200, adjusted.text
        assert adjusted.json()["before_quantity"] is None
        assert adjusted.json()["after_quantity"] == 10
        assert adjusted.json()["reason"] == "Verified physical count"
        reviewed = (await client.get(path)).json()
        assert reviewed["quantity"] == 10
        receipt_body = _receive(uuid.uuid4(), 5, reviewed)
        first = await client.post(path + "/receive", headers=headers, json=receipt_body)
        assert first.status_code == 200, first.text
        assert first.json()["before_quantity"] == 10
        assert first.json()["received_amount"] == 5
        assert first.json()["after_quantity"] == 15
        replay = await client.post(path + "/receive", headers=headers, json=receipt_body)
        assert replay.status_code == 200
        assert replay.json()["movement_id"] == first.json()["movement_id"]
        assert (await client.get(path)).json()["quantity"] == 15
        assert (await client.get(f"/api/v1/operator/product-offers/{item_id}")).json()["inventory_status"] == "available"
        activity = await client.get(path + "/activity?limit=2")
        assert activity.status_code == 200
        assert [item["operation_kind"] for item in activity.json()["items"]] == ["receive", "adjust"]
        assert activity.json()["items"][0]["actor_account_id"] == str(administrator_id)
        legacy = await client.put(path, headers=headers, json={"quantity": 0})
        assert legacy.status_code == 200
        assert legacy.json()["quantity"] == 0
        assert (await client.get(f"/api/v1/operator/product-offers/{item_id}")).json()["inventory_status"] == "out_of_stock"
        assert (await client.get(path + "/activity?limit=1")).json()["items"][0]["reason"] == "Legacy absolute quantity update"
        late_replay = await client.post(path + "/receive", headers=headers, json=receipt_body)
        assert late_replay.status_code == 200
        assert late_replay.json()["movement_id"] == first.json()["movement_id"]
        assert (await client.get(path)).json()["quantity"] == 0
    async with factory() as session:
        record = await session.scalar(select(InventoryRecord).where(InventoryRecord.sellable_item_id == item_id))
        assert record is not None and record.quantity == 0
        assert await session.scalar(select(func.count()).select_from(StockMovement)) == 3
        assert await session.scalar(select(func.count()).select_from(OperatorAuditEvent).where(
            OperatorAuditEvent.action.in_([
                "commerce.stock.adjusted", "commerce.stock.received", "commerce.inventory_quantity.changed",
            ])
        )) == 3


@pytest.mark.asyncio
async def test_unknown_invalid_stale_and_conflicting_keys_do_not_change_stock(stock) -> None:
    transport, factory, _administrator_id, item_id = stock
    path = _path(item_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        headers = _headers(csrf)
        initial = (await client.get(path)).json()
        assert (await client.post(path + "/receive", headers=headers, json={
            "operation_key": str(uuid.uuid4()), "received_amount": 5,
            "expected_quantity": 0, "expected_updated_at": None,
        })).status_code == 409
        for amount in [0, -1]:
            assert (await client.post(path + "/receive", headers=headers, json={
                "operation_key": str(uuid.uuid4()), "received_amount": amount,
                "expected_quantity": 0, "expected_updated_at": None,
            })).status_code == 422
        for reason in ["", "   "]:
            assert (await client.post(path + "/adjust", headers=headers, json=_adjust(
                uuid.uuid4(), 10, initial, reason,
            ))).status_code == 422
        assert (await client.post(path + "/adjust", headers=headers, json=_adjust(
            uuid.uuid4(), -1, initial,
        ))).status_code == 422
        established = await client.post(path + "/adjust", headers=headers, json=_adjust(uuid.uuid4(), 10, initial))
        assert established.status_code == 200
        assert (await client.post(path + "/adjust", headers=headers, json=_adjust(uuid.uuid4(), 20, initial))).status_code == 409
        reviewed = (await client.get(path)).json()
        repeated_count = await client.post(path + "/adjust", headers=headers, json=_adjust(
            uuid.uuid4(), 10, reviewed, "Recount confirmed unchanged",
        ))
        assert repeated_count.status_code == 200
        assert (await client.get(path)).json()["updated_at"] != reviewed["updated_at"]
        assert (await client.post(path + "/adjust", headers=headers, json=_adjust(
            uuid.uuid4(), 10, reviewed, "Stale same-count request",
        ))).status_code == 409
        reviewed = (await client.get(path)).json()
        receipt = _receive(uuid.uuid4(), 5, reviewed)
        accepted = await client.post(path + "/receive", headers=headers, json=receipt)
        assert accepted.status_code == 200
        assert (await client.post(path + "/receive", headers=headers, json=_receive(
            uuid.uuid4(), 5, reviewed,
        ))).status_code == 409
        assert (await client.post(path + "/receive", headers=headers, json={
            **receipt, "received_amount": 8,
        })).status_code == 409
        assert (await client.get(path)).json()["quantity"] == 15
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(StockMovement)) == 3


@pytest.mark.asyncio
async def test_concurrent_receipts_serialize_and_conflict(stock) -> None:
    transport, _factory, _administrator_id, item_id = stock
    path = _path(item_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        headers = _headers(csrf)
        initial = (await client.get(path)).json()
        assert (await client.post(path + "/adjust", headers=headers, json=_adjust(
            uuid.uuid4(), 10, initial,
        ))).status_code == 200
        reviewed = (await client.get(path)).json()
        results = await asyncio.gather(*[
            client.post(path + "/receive", headers=headers, json=_receive(uuid.uuid4(), 5, reviewed))
            for _ in range(2)
        ])
        assert sorted(result.status_code for result in results) == [200, 409]
        assert (await client.get(path)).json()["quantity"] == 15


@pytest.mark.asyncio
async def test_stock_commands_keep_browser_security_and_administrator_only_reads(stock, monkeypatch) -> None:
    transport, factory, _administrator_id, item_id = stock
    path = _path(item_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator:
        csrf = await _login(operator, "commerce.operator", OPERATOR_PASSWORD)
        assert (await operator.get(path + "/activity")).status_code == 403
        assert (await operator.post(path + "/adjust", headers=_headers(csrf), json={
            "operation_key": str(uuid.uuid4()), "corrected_quantity": 10,
            "expected_quantity": None, "expected_updated_at": None, "reason": "Count",
        })).status_code == 403
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as admin:
        csrf = await _login(admin, "commerce.admin", ADMIN_PASSWORD)
        initial = (await admin.get(path)).json()
        body = _adjust(uuid.uuid4(), 10, initial)
        assert (await admin.post(path + "/adjust", json=body)).status_code == 403
        assert (await admin.post(path + "/adjust", headers=_headers(
            csrf, origin="https://attacker.example",
        ), json=body)).status_code == 403
        assert (await admin.get(path + "/activity?limit=51")).status_code == 422
        assert (await admin.post(path + "/adjust", headers=_headers(csrf), json=body)).status_code == 200
        next_state = (await admin.get(path)).json()
        future = time.time() + 1000
        monkeypatch.setattr("app.api.browser_auth_deps.time.time", lambda: future)
        expired = await admin.post(path + "/receive", headers=_headers(csrf), json=_receive(
            uuid.uuid4(), 5, next_state,
        ))
        assert expired.status_code == 403
        assert expired.json()["error"]["code"] == "recent_reauthentication_required"
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(StockMovement)) == 1


@pytest.mark.asyncio
async def test_movement_rows_reject_update_and_delete(stock) -> None:
    transport, factory, _administrator_id, item_id = stock
    path = _path(item_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        before = (await client.get(path)).json()
        created = await client.post(path + "/adjust", headers=_headers(csrf), json=_adjust(
            uuid.uuid4(), 10, before,
        ))
        assert created.status_code == 200
        movement_id = created.json()["movement_id"]
    async with factory() as session:
        with pytest.raises(DBAPIError):
            await session.execute(text(
                "UPDATE mbb.stock_movements SET reason = 'tampered' WHERE movement_id = :id"
            ), {"id": movement_id})
        await session.rollback()
        with pytest.raises(DBAPIError):
            await session.execute(text(
                "DELETE FROM mbb.stock_movements WHERE movement_id = :id"
            ), {"id": movement_id})
        await session.rollback()
        assert await session.scalar(select(func.count()).select_from(StockMovement)) == 1


@pytest.mark.asyncio
async def test_existing_null_inventory_requires_adjust_before_receive(stock) -> None:
    transport, _factory, _administrator_id, item_id = stock
    path = _path(item_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        headers = _headers(csrf)
        assert (await client.put(path, headers=headers, json={"quantity": None})).status_code == 200
        unknown = (await client.get(path)).json()
        assert unknown["quantity"] is None and unknown["updated_at"] is not None
        blocked = await client.post(path + "/receive", headers=headers, json={
            "operation_key": str(uuid.uuid4()), "received_amount": 3,
            "expected_quantity": 0, "expected_updated_at": unknown["updated_at"],
        })
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "STOCK_QUANTITY_UNKNOWN"
        established = await client.post(path + "/adjust", headers=headers, json=_adjust(
            uuid.uuid4(), 7, unknown,
        ))
        assert established.status_code == 200
        known = (await client.get(path)).json()
        assert (await client.post(path + "/receive", headers=headers, json=_receive(
            uuid.uuid4(), 3, known,
        ))).status_code == 200
        assert (await client.get(path)).json()["quantity"] == 10
