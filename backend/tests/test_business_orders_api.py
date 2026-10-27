"""Browser-session Orders reads and safe cancellation in disposable PostgreSQL."""

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError

from app import adapters
from app.ai.commercial_state import read_commercial_state
from app.api.v1 import business_orders
from app.models.inventory import InventoryRecord
from app.models.message import Message
from app.models.operator_audit import OperatorAuditEvent
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.models.order_delivery_confirmation import OrderDeliveryConfirmation
from app.models.order_delivery_issue import OrderDeliveryIssue
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.models.stock_movement import StockMovement
from app.models.pricing import SellableItemPrice
from app.modules.m7_conversion import (
    order_cancellation,
    order_delivery,
    service as conversion_service,
)
from app.modules.m7_conversion.order_allocation import allocate_paid_order_stock
from app.modules.m7_conversion.order_delivery import (
    OrderDeliveryEvidenceAmbiguous,
    OrderDeliveryEvidenceInvalid,
    OrderDeliveryEvidenceMissing,
    OrderDeliveryEvidenceReused,
    OrderDeliveryIdempotencyConflict,
    OrderDeliveryResult,
    OrderDeliveryStale,
    OrderDeliveryStateConflict,
    OrderDeliveryUnavailable,
    confirm_delivered_order,
)
from app.modules.m7_conversion.order_dispatch import dispatch_prepared_order
from app.modules.m7_conversion.order_fulfillment import prepare_confirmed_order
from app.modules.m7_conversion.order_drafts import prepare_order_draft
from app.tasks import celery_app
from test_commerce_admin_api import (
    ADMIN_PASSWORD,
    OPERATOR_PASSWORD,
    ORIGIN,
    _headers,
    _login,
    harness,  # noqa: F401 -- disposable DB and real browser-session fixture
)
from test_order_drafts_postgres import _prepare, _reply, _seed

pytestmark = pytest.mark.skipif(
    not os.environ.get("AI2B_TEST_DATABASE_URL"),
    reason="Disposable PostgreSQL required",
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
            code = await db.scalar(
                select(OrderDraft.confirmation_code).where(
                    OrderDraft.draft_id == prepared.draft_id
                )
            )
        result, _ = await _reply(factory, seeded, content=f"OUI {code}")
        assert (
            result is not None
            and result.state == "confirmed"
            and result.order_id is not None
        )
        yield transport, factory, seeded, result.order_id
    finally:
        async with factory() as db:
            await db.execute(
                text("TRUNCATE TABLE mbb.customers RESTART IDENTITY CASCADE")
            )
            await db.commit()


async def _reauthenticate(
    client: httpx.AsyncClient,
    csrf: str,
    password: str = ADMIN_PASSWORD,
) -> str:
    response = await client.post(
        "/api/v1/auth/reauthenticate",
        headers=_headers(csrf),
        json={"password": password},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _cancel_headers(csrf: str, key: uuid.UUID) -> dict[str, str]:
    return {**_headers(csrf), "Idempotency-Key": str(key)}


def _cancel_body(updated_at, reason: str = "customer_requested") -> dict[str, str]:
    return {
        "reason_code": reason,
        "expected_updated_at": updated_at.isoformat(),
    }


async def _make_ready_for_preparation(factory, order_id: uuid.UUID):
    async with factory() as db:
        order = await db.get(Order, order_id)
        order.status = "confirmed"
        order.confirmed_at = datetime.now(timezone.utc)
        db.add(
            Payment(
                order_id=order_id,
                method="cash",
                amount=order.total_amount,
                currency=order.currency,
                status="completed",
                completed_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()
        await allocate_paid_order_stock(db, order_id=order_id)
        await db.commit()
        return order.updated_at


async def _make_prepared_for_dispatch(factory, order_id: uuid.UUID):
    reviewed_at = await _make_ready_for_preparation(factory, order_id)
    async with factory() as db:
        result = await prepare_confirmed_order(
            db,
            order_id=order_id,
            expected_updated_at=reviewed_at,
            idempotency_key=uuid.uuid4(),
            actor_kind="service",
            actor_display_name="business-orders-dispatch-fixture",
        )
        return result.order.updated_at


async def _make_delivering(factory, order_id: uuid.UUID):
    reviewed_at = await _make_prepared_for_dispatch(factory, order_id)
    async with factory() as db:
        administrator = await db.scalar(
            select(OperatorAccount).where(
                OperatorAccount.username_normalized == "commerce.admin"
            )
        )
        result = await dispatch_prepared_order(
            db,
            order_id=order_id,
            expected_updated_at=reviewed_at,
            idempotency_key=uuid.uuid4(),
            handoff_reference=f"DELIVERY-TEST-{str(order_id)[:8]}",
            attested=True,
            actor_account_id=administrator.account_id,
            actor_display_name=administrator.display_name,
            actor_role=administrator.role,
            request_id="delivery-test-dispatch",
        )
        return result.order.updated_at, result.movement


async def _receipt_message(
    factory,
    conversation_id: uuid.UUID,
    *,
    content: str = "Commande reçue, matondo mingi — nimepokea vizuri.",
    created_at: datetime | None = None,
):
    message_id = uuid.uuid4()
    received_at = created_at or datetime.now(timezone.utc)
    async with factory() as db:
        values = dict(
            message_id=message_id,
            conversation_id=conversation_id,
            timestamp=received_at,
            direction="inbound",
            content=content,
            content_type="text",
            language="french",
            whatsapp_message_id=f"delivery-receipt-{message_id}",
        )
        if created_at is not None:
            values["created_at"] = created_at
        message = Message(**values)
        db.add(message)
        await db.commit()
        await db.refresh(message)
        return message


async def _next_accepted_order(factory, seeded, *, quantity: int = 1):
    source = await _receipt_message(
        factory,
        seeded.conversation_id,
        content="Je veux une autre commande du même produit.",
    )
    async with factory() as db:
        state = await read_commercial_state(db, seeded.conversation_id)
        prepared = await prepare_order_draft(
            db,
            conversation_id=seeded.conversation_id,
            source_message_id=source.message_id,
            turn_id=uuid.uuid4(),
            expected_ownership_version=1,
            expected_commercial_state_revision=0 if state is None else state.revision,
            sellable_item_id=seeded.sellable_item_id,
            quantity=quantity,
        )
        await db.commit()
    async with factory() as db:
        code = await db.scalar(
            select(OrderDraft.confirmation_code).where(
                OrderDraft.draft_id == prepared.draft_id
            )
        )
    result, _ = await _reply(factory, seeded, content=f"OUI {code}")
    assert result is not None and result.order_id is not None
    return result.order_id


async def _confirm_delivery(
    factory,
    *,
    order_id: uuid.UUID,
    reviewed_at: datetime,
    evidence_message_id: uuid.UUID,
    key: uuid.UUID | None = None,
):
    async with factory() as db:
        administrator = await db.scalar(
            select(OperatorAccount).where(
                OperatorAccount.username_normalized == "commerce.admin"
            )
        )
        return await confirm_delivered_order(
            db,
            order_id=order_id,
            expected_updated_at=reviewed_at,
            evidence_message_id=evidence_message_id,
            attested=True,
            idempotency_key=key or uuid.uuid4(),
            actor_account_id=administrator.account_id,
            actor_display_name=administrator.display_name,
            actor_role=administrator.role,
            request_id="delivery-confirmation-test",
        )


@pytest.mark.asyncio
async def test_authoritative_snapshot_survives_current_commercial_changes(
    accepted_order,
):
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
        assert (
            listing.status_code == 200
            and listing.headers["cache-control"] == "no-store"
        )
        assert listing.json()["has_more"] is False
        assert [item["order_id"] for item in listing.json()["items"]] == [str(order_id)]
        detail = await client.get(f"/api/v1/business/orders/{order_id}")
        assert (
            detail.status_code == 200 and detail.headers["cache-control"] == "no-store"
        )
        body = detail.json()
        assert body["status"] == "pending" and body["conversation_id"] == str(
            seeded.conversation_id
        )
        assert Decimal(body["total_cdf"]) == original_total
        assert body["items"] == original_items
        assert body["items"][0]["price_id"] == str(seeded.price_id)
        assert body["items"][0]["sellable_item_id"] == str(seeded.sellable_item_id)
        assert body["preparation"] == {
            "eligible": False,
            "order_state": "not_confirmed",
            "payment_state": "missing",
            "allocation_state": "missing",
            "required_quantity": 2,
            "allocated_quantity": None,
        }
        assert body["dispatch"] == {
            "eligible": False,
            "order_state": "not_preparing",
            "payment_state": "missing",
            "allocation_state": "missing",
            "inventory_state": "conflict",
            "delivery_method_state": "supported",
            "required_quantity": 2,
        }
        assert body["delivered_at"] is None
        assert body["delivery"] == {
            "eligible": False,
            "order_state": "not_delivering",
            "delivery_method_state": "supported",
            "line_state": "single",
            "dispatch_state": "missing",
            "association_state": "not_applicable",
            "evidence_state": "not_applicable",
            "issue_state": "none",
            "dispatch_occurred_at": None,
            "evidence_messages": [],
            "evidence_has_more": False,
        }
        assert body["delivery_issues"] == []
        assert Decimal(body["items"][0]["usd_to_cdf_rate"]) == Decimal("2800.000000")
        assert (await client.get("/api/v1/business/orders?status=delivered")).json()[
            "items"
        ] == []
        assert (
            await client.post("/api/v1/business/orders", json={})
        ).status_code == 405
        assert (
            await client.put(f"/api/v1/business/orders/{order_id}", json={})
        ).status_code == 405
    async with factory() as db:
        stored = await db.get(Order, order_id)
        assert stored.items == original_items and stored.total_amount == original_total
        assert stored.updated_at == original_updated_at and stored.status == "pending"
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert (await db.get(InventoryRecord, seeded.inventory_id)).quantity == 0


@pytest.mark.asyncio
async def test_administrator_prepares_ready_order_with_browser_guard_and_audit(
    accepted_order,
):
    transport, factory, _seeded, order_id = accepted_order
    reviewed_at = await _make_ready_for_preparation(factory, order_id)
    key = uuid.uuid4()
    url = f"/api/v1/business/orders/{order_id}/prepare"

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        detail = await client.get(f"/api/v1/business/orders/{order_id}")
        assert detail.status_code == 200
        assert detail.json()["preparation"] == {
            "eligible": True,
            "order_state": "confirmed",
            "payment_state": "verified",
            "allocation_state": "full",
            "required_quantity": 2,
            "allocated_quantity": 2,
        }
        first = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json={"expected_updated_at": reviewed_at.isoformat()},
        )
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["status"] == "preparing"
        assert first.json()["replayed"] is False

        replay = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json={"expected_updated_at": reviewed_at.isoformat()},
        )
        assert replay.status_code == 200 and replay.json()["replayed"] is True

    async with factory() as db:
        order = await db.get(Order, order_id)
        audit = await db.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.action == "order.prepared",
                OperatorAuditEvent.target_id == str(order_id),
            )
        )
        allocation = await db.get(StockAllocation, order_id)
        assert order.status == "preparing" and order.updated_at > reviewed_at
        assert allocation.quantity == 2
        assert audit.actor_kind == "human"
        assert audit.actor_display_name == "Commerce.Admin"
        assert audit.effective_role == "administrator"


@pytest.mark.asyncio
async def test_preparation_browser_permissions_and_stale_write_are_enforced(
    accepted_order,
):
    transport, factory, _seeded, order_id = accepted_order
    reviewed_at = await _make_ready_for_preparation(factory, order_id)
    url = f"/api/v1/business/orders/{order_id}/prepare"
    body = {"expected_updated_at": reviewed_at.isoformat()}

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        operator_csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        forbidden = await client.post(
            url,
            headers={**_headers(operator_csrf), "Idempotency-Key": str(uuid.uuid4())},
            json=body,
        )
        assert forbidden.status_code == 403

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        admin_csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        stale = await client.post(
            url,
            headers={**_headers(admin_csrf), "Idempotency-Key": str(uuid.uuid4())},
            json={
                "expected_updated_at": (
                    reviewed_at - timedelta(microseconds=1)
                ).isoformat()
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "ORDER_STALE"

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "confirmed"
        assert await db.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.prepared")
        ) == 0


@pytest.mark.asyncio
async def test_administrator_dispatch_requires_reauth_and_deducts_stock_once(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, seeded, order_id = accepted_order
    key = uuid.uuid4()
    url = f"/api/v1/business/orders/{order_id}/dispatch"

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        inventory_url = (
            f"/api/v1/operator/commerce/sellable-items/"
            f"{seeded.sellable_item_id}/inventory"
        )
        initial = (await client.get(inventory_url)).json()
        adjusted = await client.post(
            inventory_url + "/adjust",
            headers=_headers(csrf),
            json={
                "operation_key": str(uuid.uuid4()),
                "corrected_quantity": 4,
                "expected_quantity": initial["quantity"],
                "expected_updated_at": initial["updated_at"],
                "reason": "Mixed activity dispatch regression",
            },
        )
        assert adjusted.status_code == 200, adjusted.text
        received = await client.post(
            inventory_url + "/receive",
            headers=_headers(csrf),
            json={
                "operation_key": str(uuid.uuid4()),
                "received_amount": 1,
                "expected_quantity": adjusted.json()["after_quantity"],
                "expected_updated_at": adjusted.json()["occurred_at"],
            },
        )
        assert received.status_code == 200, received.text
        reviewed_at = await _make_prepared_for_dispatch(factory, order_id)
        body = {
            "expected_updated_at": reviewed_at.isoformat(),
            "handoff_reference": "MBB-HANDOFF-API-1",
            "attested": True,
        }
        detail = await client.get(f"/api/v1/business/orders/{order_id}")
        assert detail.status_code == 200
        assert detail.json()["dispatch"] == {
            "eligible": True,
            "order_state": "preparing",
            "payment_state": "verified",
            "allocation_state": "full_unconsumed",
            "inventory_state": "ready",
            "delivery_method_state": "supported",
            "required_quantity": 2,
        }
        future = datetime.now(timezone.utc).timestamp() + 1000
        monkeypatch.setattr("app.api.browser_auth_deps.time.time", lambda: future)
        reauth_required = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json=body,
        )
        assert reauth_required.status_code == 403
        assert reauth_required.json()["error"]["code"] == "recent_reauthentication_required"
        csrf = await _reauthenticate(client, csrf)
        first = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json=body,
        )
        assert first.status_code == 200
        assert first.json()["status"] == "delivering"
        assert first.json()["replayed"] is False
        replay = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json=body,
        )
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        activity = await client.get(inventory_url + "/activity")
        assert activity.status_code == 200, activity.text
        movements = activity.json()["items"]
        assert [item["operation_kind"] for item in movements[:3]] == [
            "dispatch", "receive", "adjust",
        ]
        assert movements[0] == {
            "movement_id": movements[0]["movement_id"],
            "sellable_item_id": str(seeded.sellable_item_id),
            "operation_kind": "dispatch",
            "order_id": str(order_id),
            "before_quantity": 5,
            "before_updated_at": movements[0]["before_updated_at"],
            "received_amount": None,
            "corrected_quantity": None,
            "dispatched_quantity": 2,
            "after_quantity": 3,
            "reason": None,
            "handoff_reference": "MBB-HANDOFF-API-1",
            "actor_account_id": movements[0]["actor_account_id"],
            "operation_key": str(key),
            "occurred_at": movements[0]["occurred_at"],
        }

    async with factory() as db:
        order = await db.get(Order, order_id)
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        movement = await db.scalar(
            select(StockMovement).where(StockMovement.order_id == order_id)
        )
        assert order.status == "delivering" and order.delivered_at is None
        assert inventory.quantity == 3 and inventory.allocated_quantity == 0
        assert movement.handoff_reference == "MBB-HANDOFF-API-1"
        assert await db.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.dispatched")
        ) == 1


@pytest.mark.asyncio
async def test_operator_cannot_dispatch_prepared_order(accepted_order):
    transport, factory, _seeded, order_id = accepted_order
    reviewed_at = await _make_prepared_for_dispatch(factory, order_id)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        response = await client.post(
            f"/api/v1/business/orders/{order_id}/dispatch",
            headers={**_headers(csrf), "Idempotency-Key": str(uuid.uuid4())},
            json={
                "expected_updated_at": reviewed_at.isoformat(),
                "handoff_reference": "MBB-HANDOFF-DENIED",
                "attested": True,
            },
        )
        assert response.status_code == 403
    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "preparing"
        assert await db.scalar(
            select(func.count())
            .select_from(StockMovement)
            .where(StockMovement.operation_kind == "dispatch")
        ) == 0


@pytest.mark.asyncio
async def test_administrator_confirms_delivery_with_reauth_replay_and_no_commerce_mutation(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(microseconds=1),
    )
    key = uuid.uuid4()
    url = f"/api/v1/business/orders/{order_id}/confirm-delivery"
    body = {
        "expected_updated_at": reviewed_at.isoformat(),
        "evidence_message_id": str(evidence.message_id),
        "attested": True,
    }
    async with factory() as db:
        payment_before = await db.scalar(
            select(Payment).where(Payment.order_id == order_id)
        )
        payment_snapshot = (
            payment_before.status,
            payment_before.amount,
            payment_before.completed_at,
        )
        inventory_before = await db.get(InventoryRecord, seeded.inventory_id)
        stock_snapshot = (
            inventory_before.quantity,
            inventory_before.allocated_quantity,
            inventory_before.updated_at,
        )
        movement_count = await db.scalar(
            select(func.count()).select_from(StockMovement)
        )

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        detail = await client.get(f"/api/v1/business/orders/{order_id}")
        assert detail.status_code == 200
        delivery = detail.json()["delivery"]
        assert delivery["eligible"] is True
        assert delivery["order_state"] == "delivering"
        assert delivery["dispatch_state"] == "matched"
        assert delivery["association_state"] == "verified"
        assert delivery["evidence_state"] == "available"
        assert delivery["evidence_has_more"] is False
        assert delivery["evidence_messages"] == [
            {
                "message_id": str(evidence.message_id),
                "content_excerpt": evidence.content,
                "content_truncated": False,
                "received_at": evidence.created_at.isoformat().replace("+00:00", "Z"),
                "inbound_receipt_order": evidence.inbound_receipt_order,
                "provenance": "inbound_whatsapp_customer_message",
            }
        ]
        future = datetime.now(timezone.utc).timestamp() + 1000
        monkeypatch.setattr("app.api.browser_auth_deps.time.time", lambda: future)
        denied = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json=body,
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "recent_reauthentication_required"
        csrf = await _reauthenticate(client, csrf)
        first = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json=body,
        )
        assert first.status_code == 200, first.text
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["status"] == "delivered"
        assert first.json()["delivered_at"] == evidence.created_at.isoformat().replace(
            "+00:00", "Z"
        )
        assert first.json()["replayed"] is False
        replay = await client.post(
            url,
            headers={**_headers(csrf), "Idempotency-Key": str(key)},
            json=body,
        )
        assert replay.status_code == 200 and replay.json()["replayed"] is True

    async with factory() as db:
        order = await db.get(Order, order_id)
        confirmation = await db.scalar(
            select(OrderDeliveryConfirmation).where(
                OrderDeliveryConfirmation.order_id == order_id
            )
        )
        audit = await db.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.action == "order.delivered",
                OperatorAuditEvent.target_id == str(order_id),
            )
        )
        payment_after = await db.scalar(
            select(Payment).where(Payment.order_id == order_id)
        )
        inventory_after = await db.get(InventoryRecord, seeded.inventory_id)
        assert order.status == "delivered" and order.delivered_at == evidence.created_at
        assert confirmation.evidence_message_id == evidence.message_id
        assert confirmation.dispatch_movement_id == movement.movement_id
        assert audit.event_metadata["confirmation_id"] == str(
            confirmation.confirmation_id
        )
        assert audit.event_metadata["evidence_message_id"] == str(evidence.message_id)
        assert audit.event_metadata["attested"] is True
        assert (
            payment_after.status,
            payment_after.amount,
            payment_after.completed_at,
        ) == payment_snapshot
        assert (
            inventory_after.quantity,
            inventory_after.allocated_quantity,
            inventory_after.updated_at,
        ) == stock_snapshot
        assert (
            await db.scalar(select(func.count()).select_from(StockMovement))
            == movement_count
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.delivered")
            )
            == 1
        )
        with pytest.raises(DBAPIError, match="delivery confirmations are append-only"):
            await db.execute(
                update(OrderDeliveryConfirmation)
                .where(OrderDeliveryConfirmation.order_id == order_id)
                .values(evidence_fingerprint="0" * 64)
            )


@pytest.mark.asyncio
async def test_operator_cannot_confirm_delivery(accepted_order):
    transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(microseconds=1),
    )
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        response = await client.post(
            f"/api/v1/business/orders/{order_id}/confirm-delivery",
            headers={**_headers(csrf), "Idempotency-Key": str(uuid.uuid4())},
            json={
                "expected_updated_at": reviewed_at.isoformat(),
                "evidence_message_id": str(evidence.message_id),
                "attested": True,
            },
        )
        assert response.status_code == 403
    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "delivering"
        assert (
            await db.scalar(select(func.count()).select_from(OrderDeliveryConfirmation))
            == 0
        )


@pytest.mark.asyncio
async def test_delivery_evidence_read_is_bounded_ordered_and_truncated(accepted_order):
    transport, factory, seeded, order_id = accepted_order
    _reviewed_at, movement = await _make_delivering(factory, order_id)
    async with factory() as db:
        for index in range(21):
            received_at = movement.occurred_at + timedelta(seconds=index + 1)
            db.add(
                Message(
                    message_id=uuid.uuid4(),
                    conversation_id=seeded.conversation_id,
                    timestamp=received_at,
                    created_at=received_at,
                    direction="inbound",
                    content=("x" * 501 if index == 20 else f"Receipt candidate {index}"),
                    content_type="text",
                    language="french",
                    whatsapp_message_id=f"bounded-delivery-evidence-{index}",
                )
            )
        await db.commit()

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        response = await client.get(f"/api/v1/business/orders/{order_id}")
        assert response.status_code == 200
        delivery = response.json()["delivery"]
        assert delivery["eligible"] is True
        assert delivery["evidence_has_more"] is True
        assert len(delivery["evidence_messages"]) == 20
        newest = delivery["evidence_messages"][0]
        assert newest["content_excerpt"] == "x" * 500
        assert newest["content_truncated"] is True
        assert newest["provenance"] == "inbound_whatsapp_customer_message"
        receipt_orders = [
            message["inbound_receipt_order"]
            for message in delivery["evidence_messages"]
        ]
        assert receipt_orders == sorted(receipt_orders, reverse=True)


@pytest.mark.asyncio
async def test_delivery_rejects_missing_and_pre_dispatch_evidence(accepted_order):
    _transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    with pytest.raises(OrderDeliveryEvidenceMissing):
        await _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at,
            evidence_message_id=uuid.uuid4(),
        )
    pre_dispatch = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at - timedelta(seconds=1),
    )
    with pytest.raises(OrderDeliveryEvidenceInvalid):
        await _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at,
            evidence_message_id=pre_dispatch.message_id,
        )
    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "delivering"
        assert (
            await db.scalar(select(func.count()).select_from(OrderDeliveryConfirmation))
            == 0
        )


@pytest.mark.asyncio
async def test_delivery_rejects_stale_write_and_changed_idempotent_request(
    accepted_order,
):
    _transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(microseconds=1),
    )
    with pytest.raises(OrderDeliveryStale):
        await _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at - timedelta(microseconds=1),
            evidence_message_id=evidence.message_id,
        )
    key = uuid.uuid4()
    await _confirm_delivery(
        factory,
        order_id=order_id,
        reviewed_at=reviewed_at,
        evidence_message_id=evidence.message_id,
        key=key,
    )
    changed_evidence = await _receipt_message(factory, seeded.conversation_id)
    with pytest.raises(OrderDeliveryIdempotencyConflict):
        await _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at,
            evidence_message_id=changed_evidence.message_id,
            key=key,
        )


@pytest.mark.asyncio
async def test_delivery_serializes_competing_confirmations(accepted_order):
    _transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(microseconds=1),
    )
    results = await asyncio.gather(
        _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at,
            evidence_message_id=evidence.message_id,
        ),
        _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at,
            evidence_message_id=evidence.message_id,
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, OrderDeliveryResult) for result in results) == 1
    assert any(isinstance(result, OrderDeliveryStateConflict) for result in results)
    async with factory() as db:
        assert (
            await db.scalar(select(func.count()).select_from(OrderDeliveryConfirmation))
            == 1
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.delivered")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_delivery_rolls_back_order_evidence_and_audit_together(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    _transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(microseconds=1),
    )

    async def fail_audit(*args, **kwargs):
        raise RuntimeError("synthetic delivery audit failure")

    monkeypatch.setattr(order_delivery, "append_operator_audit_event", fail_audit)
    with pytest.raises(OrderDeliveryUnavailable):
        await _confirm_delivery(
            factory,
            order_id=order_id,
            reviewed_at=reviewed_at,
            evidence_message_id=evidence.message_id,
        )
    async with factory() as db:
        order = await db.get(Order, order_id)
        assert order.status == "delivering" and order.delivered_at is None
        assert (
            await db.scalar(select(func.count()).select_from(OrderDeliveryConfirmation))
            == 0
        )
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.delivered")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_delivery_rejects_ambiguous_and_reused_customer_evidence(accepted_order):
    _transport, factory, seeded, first_order_id = accepted_order
    first_reviewed_at, first_movement = await _make_delivering(factory, first_order_id)
    second_order_id = await _next_accepted_order(factory, seeded)
    second_reviewed_at, second_movement = await _make_delivering(factory, second_order_id)
    first_evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=max(first_movement.occurred_at, second_movement.occurred_at)
        + timedelta(microseconds=1),
    )

    with pytest.raises(OrderDeliveryEvidenceAmbiguous):
        await _confirm_delivery(
            factory,
            order_id=first_order_id,
            reviewed_at=first_reviewed_at,
            evidence_message_id=first_evidence.message_id,
        )

    async with factory() as db:
        second = await db.get(Order, second_order_id)
        second.status = "delivered"
        second.delivered_at = datetime.now(timezone.utc)
        second.updated_at = second.delivered_at
        await db.commit()
    await _confirm_delivery(
        factory,
        order_id=first_order_id,
        reviewed_at=first_reviewed_at,
        evidence_message_id=first_evidence.message_id,
    )
    async with factory() as db:
        second = await db.get(Order, second_order_id)
        second.status = "delivering"
        second.delivered_at = None
        second.updated_at = datetime.now(timezone.utc)
        await db.commit()
        reused_reviewed_at = second.updated_at
    with pytest.raises(OrderDeliveryEvidenceReused):
        await _confirm_delivery(
            factory,
            order_id=second_order_id,
            reviewed_at=reused_reviewed_at,
            evidence_message_id=first_evidence.message_id,
        )


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
            assert (
                await client.get(f"/api/v1/business/orders?{query}")
            ).status_code == 422
        assert (
            await client.get("/api/v1/business/orders/not-a-uuid")
        ).status_code == 422
        async with factory() as db:
            draft = await db.scalar(
                select(OrderDraft).where(OrderDraft.order_id == order_id)
            )
            draft.order_id = None
            await db.commit()
        assert (await client.get(url)).json()["conversation_id"] is None
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        async with factory() as db:
            account = await db.scalar(
                select(OperatorAccount).where(
                    OperatorAccount.username_normalized == "commerce.operator"
                )
            )
            account.role = "analyst"
            await db.commit()
        assert (await client.get(url)).status_code == 403


@pytest.mark.asyncio
async def test_administrator_cancels_once_with_atomic_audit_and_no_side_effects(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, seeded, order_id = accepted_order
    external_calls: list[str] = []

    def blocked(name: str):
        def record(*_args, **_kwargs):
            external_calls.append(name)
            pytest.fail(f"unexpected external effect: {name}")

        return record

    monkeypatch.setattr(adapters, "get_payment_adapter", blocked("payment"))
    monkeypatch.setattr(adapters, "get_crm_adapter", blocked("crm"))
    monkeypatch.setattr(adapters, "get_messaging_adapter", blocked("messaging"))
    monkeypatch.setattr(celery_app.celery_app, "send_task", blocked("celery"))

    async with factory() as db:
        order = await db.get(Order, order_id)
        reviewed_at = order.updated_at
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        inventory_before = (inventory.quantity, inventory.status, inventory.updated_at)

    key = uuid.uuid4()
    url = f"/api/v1/business/orders/{order_id}/cancel"
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)
        first = await client.post(
            url,
            headers=_cancel_headers(csrf, key),
            json=_cancel_body(reviewed_at),
        )
        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["status"] == "cancelled"
        assert first.json()["replayed"] is False

        replay = await client.post(
            url,
            headers=_cancel_headers(csrf, key),
            json=_cancel_body(reviewed_at),
        )
        assert replay.status_code == 200
        assert replay.json()["replayed"] is True

        changed_request = await client.post(
            url,
            headers=_cancel_headers(csrf, key),
            json=_cancel_body(reviewed_at, "duplicate_order"),
        )
        assert changed_request.status_code == 409
        assert changed_request.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"

    async with factory() as db:
        stored = await db.get(Order, order_id)
        assert stored.status == "cancelled" and stored.updated_at > reviewed_at
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        assert (
            inventory.quantity,
            inventory.status,
            inventory.updated_at,
        ) == inventory_before
        audits = list(
            (
                await db.scalars(
                    select(OperatorAuditEvent).where(
                        OperatorAuditEvent.action == "order.cancelled",
                        OperatorAuditEvent.target_id == str(order_id),
                    )
                )
            ).all()
        )
        assert len(audits) == 1
        assert audits[0].reason_code == "customer_requested"
        assert audits[0].effective_role == "administrator"
        assert audits[0].event_metadata["previous_status"] == "pending"
        assert audits[0].event_metadata["new_status"] == "cancelled"
        assert audits[0].idempotency_reference is not None
    assert external_calls == []


@pytest.mark.asyncio
async def test_cancellation_updated_at_advances_from_locked_future_value(
    accepted_order,
):
    transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        order = await db.get(Order, order_id)
        reviewed_at = await db.scalar(
            select(func.clock_timestamp() + timedelta(days=1))
        )
        assert reviewed_at is not None
        order.updated_at = reviewed_at
        await db.commit()

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)
        response = await client.post(
            f"/api/v1/business/orders/{order_id}/cancel",
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=_cancel_body(reviewed_at),
        )

    assert response.status_code == 200
    async with factory() as db:
        stored = await db.get(Order, order_id)
        audit = await db.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.action == "order.cancelled",
                OperatorAuditEvent.target_id == str(order_id),
            )
        )
        assert stored.status == "cancelled"
        assert stored.updated_at == reviewed_at + timedelta(microseconds=1)
        assert audit is not None and audit.occurred_at == stored.updated_at


@pytest.mark.asyncio
async def test_cancellation_requires_administrator_and_csrf(
    accepted_order,
):
    transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        reviewed_at = (await db.get(Order, order_id)).updated_at
    url = f"/api/v1/business/orders/{order_id}/cancel"
    body = _cancel_body(reviewed_at)

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        operator_csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        denied = await client.post(
            url,
            headers=_cancel_headers(operator_csrf, uuid.uuid4()),
            json=body,
        )
        assert denied.status_code == 403

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.admin", ADMIN_PASSWORD)
        no_csrf = await client.post(
            url,
            headers={
                "Origin": ORIGIN,
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json=body,
        )
        assert no_csrf.status_code == 403
        assert no_csrf.json()["error"]["code"] == "csrf_invalid"

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "pending"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_stale_terminal_and_payment_states_fail_closed(accepted_order):
    transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        order = await db.get(Order, order_id)
        reviewed_at = order.updated_at
        order.updated_at = reviewed_at + timedelta(seconds=1)
        await db.commit()

    url = f"/api/v1/business/orders/{order_id}/cancel"
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)
        stale = await client.post(
            url,
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=_cancel_body(reviewed_at),
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "ORDER_STALE"

        async with factory() as db:
            order = await db.get(Order, order_id)
            current = order.updated_at
            db.add(
                Payment(
                    payment_id=uuid.uuid4(),
                    order_id=order_id,
                    method="orange_money",
                    amount=order.total_amount,
                    currency="CDF",
                    status="pending",
                )
            )
            await db.commit()
        payment_blocked = await client.post(
            url,
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=_cancel_body(current),
        )
        assert payment_blocked.status_code == 409
        assert payment_blocked.json()["error"]["code"] == "ORDER_PAYMENT_CONFLICT"

        async with factory() as db:
            payment = await db.scalar(
                select(Payment).where(Payment.order_id == order_id)
            )
            assert payment.status == "pending"
            payment.status = "completed"
            await db.commit()
        completed_blocked = await client.post(
            url,
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=_cancel_body(current),
        )
        assert completed_blocked.status_code == 409
        assert completed_blocked.json()["error"]["code"] == "ORDER_PAYMENT_CONFLICT"

        async with factory() as db:
            await db.delete(
                await db.scalar(select(Payment).where(Payment.order_id == order_id))
            )
            order = await db.get(Order, order_id)
            order.status = "confirmed"
            await db.commit()
        terminal = await client.post(
            url,
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=_cancel_body(current),
        )
        assert terminal.status_code == 409
        assert terminal.json()["error"]["code"] == "ORDER_STATE_CONFLICT"

    async with factory() as db:
        stored = await db.get(Order, order_id)
        assert stored.status == "confirmed"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_concurrent_cancellation_commits_once(accepted_order):
    transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        reviewed_at = (await db.get(Order, order_id)).updated_at
    url = f"/api/v1/business/orders/{order_id}/cancel"
    key = uuid.uuid4()
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)

        async def submit():
            return await client.post(
                url,
                headers=_cancel_headers(csrf, key),
                json=_cancel_body(reviewed_at),
            )

        first, second = await asyncio.gather(submit(), submit())
        assert sorted([first.status_code, second.status_code]) == [200, 200]
        assert sorted([first.json()["replayed"], second.json()["replayed"]]) == [
            False,
            True,
        ]

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "cancelled"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 1
        )


@pytest.mark.asyncio
async def test_concurrent_distinct_cancellations_leave_one_authoritative_result(
    accepted_order,
):
    transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        reviewed_at = (await db.get(Order, order_id)).updated_at
    url = f"/api/v1/business/orders/{order_id}/cancel"
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)

        async def submit(reason: str):
            return await client.post(
                url,
                headers=_cancel_headers(csrf, uuid.uuid4()),
                json=_cancel_body(reviewed_at, reason),
            )

        first, second = await asyncio.gather(
            submit("customer_requested"), submit("duplicate_order")
        )
        assert sorted([first.status_code, second.status_code]) == [200, 409]
        conflict = first if first.status_code == 409 else second
        assert conflict.json()["error"]["code"] == "ORDER_STATE_CONFLICT"

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "cancelled"
        audits = list(
            (
                await db.scalars(
                    select(OperatorAuditEvent).where(
                        OperatorAuditEvent.action == "order.cancelled"
                    )
                )
            ).all()
        )
        assert len(audits) == 1
        assert audits[0].reason_code in {"customer_requested", "duplicate_order"}


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_cancellation(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        reviewed_at = (await db.get(Order, order_id)).updated_at

    async def fail_audit(*_args, **_kwargs):
        raise SQLAlchemyError("audit unavailable")

    monkeypatch.setattr(order_cancellation, "append_operator_audit_event", fail_audit)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)
        response = await client.post(
            f"/api/v1/business/orders/{order_id}/cancel",
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=_cancel_body(reviewed_at),
        )
        assert response.status_code == 503

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "pending"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_payment_insert_waits_for_cancellation_then_rolls_back(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, seeded, order_id = accepted_order
    entered_audit = asyncio.Event()
    release_audit = asyncio.Event()
    original_append = order_cancellation.append_operator_audit_event

    async def pause_after_order_lock(*args, **kwargs):
        entered_audit.set()
        await release_audit.wait()
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(
        order_cancellation, "append_operator_audit_event", pause_after_order_lock
    )
    async with factory() as db:
        order = await db.get(Order, order_id)
        reviewed_at = order.updated_at
        amount = order.total_amount
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        inventory_before = (inventory.quantity, inventory.status, inventory.updated_at)

    async def insert_payment():
        async with factory() as db:
            db.add(
                Payment(
                    order_id=order_id,
                    method="orange_money",
                    amount=amount,
                    currency="CDF",
                    status="pending",
                )
            )
            await db.commit()

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)
        cancellation = asyncio.create_task(
            client.post(
                f"/api/v1/business/orders/{order_id}/cancel",
                headers=_cancel_headers(csrf, uuid.uuid4()),
                json=_cancel_body(reviewed_at),
            )
        )
        await asyncio.wait_for(entered_audit.wait(), timeout=5)
        insertion = asyncio.create_task(insert_payment())
        try:
            await asyncio.sleep(0.1)
            assert not insertion.done(), "Payment insert bypassed the Order lock"
        finally:
            release_audit.set()
        response = await asyncio.wait_for(cancellation, timeout=5)
        assert response.status_code == 200
        with pytest.raises(
            IntegrityError, match="Payment cannot be attached to a cancelled Order"
        ):
            await asyncio.wait_for(insertion, timeout=5)

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "cancelled"
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 1
        )
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        assert (
            inventory.quantity,
            inventory.status,
            inventory.updated_at,
        ) == inventory_before


@pytest.mark.asyncio
@pytest.mark.parametrize("commit_payment", [True, False])
async def test_cancellation_waits_for_payment_transaction(
    accepted_order,
    commit_payment: bool,
):
    transport, factory, seeded, order_id = accepted_order
    async with factory() as db:
        order = await db.get(Order, order_id)
        reviewed_at = order.updated_at
        amount = order.total_amount
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        inventory_before = (inventory.quantity, inventory.status, inventory.updated_at)

    async with factory() as payment_db:
        payment_db.add(
            Payment(
                order_id=order_id,
                method="orange_money",
                amount=amount,
                currency="CDF",
                status="pending",
            )
        )
        await payment_db.flush()
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
            csrf = await _reauthenticate(client, csrf)
            cancellation = asyncio.create_task(
                client.post(
                    f"/api/v1/business/orders/{order_id}/cancel",
                    headers=_cancel_headers(csrf, uuid.uuid4()),
                    json=_cancel_body(reviewed_at),
                )
            )
            await asyncio.sleep(0.1)
            assert not cancellation.done(), "Cancellation bypassed the Payment lock"
            if commit_payment:
                await payment_db.commit()
            else:
                await payment_db.rollback()
            response = await asyncio.wait_for(cancellation, timeout=5)

    assert response.status_code == (409 if commit_payment else 200)
    if commit_payment:
        assert response.json()["error"]["code"] == "ORDER_PAYMENT_CONFLICT"
    async with factory() as db:
        assert (await db.get(Order, order_id)).status == (
            "pending" if commit_payment else "cancelled"
        )
        assert await db.scalar(select(func.count()).select_from(Payment)) == int(
            commit_payment
        )
        assert await db.scalar(
            select(func.count())
            .select_from(OperatorAuditEvent)
            .where(OperatorAuditEvent.action == "order.cancelled")
        ) == int(not commit_payment)
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        assert (
            inventory.quantity,
            inventory.status,
            inventory.updated_at,
        ) == inventory_before


@pytest.mark.asyncio
async def test_blocked_legacy_confirmation_preserves_cancellation_audit(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, seeded, order_id = accepted_order
    cancellation_locked = asyncio.Event()
    release_cancellation = asyncio.Event()
    original_append = order_cancellation.append_operator_audit_event

    async def pause_after_order_lock(*args, **kwargs):
        cancellation_locked.set()
        await release_cancellation.wait()
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(
        order_cancellation, "append_operator_audit_event", pause_after_order_lock
    )
    async with factory() as db:
        reviewed_at = (await db.get(Order, order_id)).updated_at
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        inventory_before = (inventory.quantity, inventory.status, inventory.updated_at)

    async def legacy_transition():
        async with factory() as db:
            assert (await db.get(Order, order_id)).status == "pending"
            await conversion_service.update_order_status(
                db, order_id=order_id, new_status="confirmed"
            )

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        csrf = await _reauthenticate(client, csrf)
        cancellation = asyncio.create_task(
            client.post(
                f"/api/v1/business/orders/{order_id}/cancel",
                headers=_cancel_headers(csrf, uuid.uuid4()),
                json=_cancel_body(reviewed_at),
            )
        )
        await asyncio.wait_for(cancellation_locked.wait(), timeout=5)
        with pytest.raises(conversion_service.PaymentPrerequisiteUnavailable):
            await asyncio.wait_for(legacy_transition(), timeout=5)
        try:
            await asyncio.sleep(0.1)
            assert not cancellation.done()
        finally:
            release_cancellation.set()
        response = await asyncio.wait_for(cancellation, timeout=5)
        assert response.status_code == 200

    async with factory() as db:
        stored = await db.get(Order, order_id)
        assert stored.status == "cancelled" and stored.confirmed_at is None
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 1
        )
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        assert (
            inventory.quantity,
            inventory.status,
            inventory.updated_at,
        ) == inventory_before


@pytest.mark.asyncio
async def test_cancellation_waits_for_order_lock_despite_blocked_legacy_confirmation(accepted_order):
    transport, factory, seeded, order_id = accepted_order
    async with factory() as db:
        reviewed_at = (await db.get(Order, order_id)).updated_at
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        inventory_before = (inventory.quantity, inventory.status, inventory.updated_at)

    async with factory() as legacy_db:
        await legacy_db.scalar(
            select(Order).where(Order.order_id == order_id).with_for_update()
        )
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
            csrf = await _reauthenticate(client, csrf)
            cancellation = asyncio.create_task(
                client.post(
                    f"/api/v1/business/orders/{order_id}/cancel",
                    headers=_cancel_headers(csrf, uuid.uuid4()),
                    json=_cancel_body(reviewed_at),
                )
            )
            await asyncio.sleep(0.1)
            assert not cancellation.done(), "Cancellation bypassed the Order lock"
            with pytest.raises(conversion_service.PaymentPrerequisiteUnavailable):
                await conversion_service.update_order_status(
                    legacy_db, order_id=order_id, new_status="confirmed"
                )
            await legacy_db.rollback()
            response = await asyncio.wait_for(cancellation, timeout=5)

    assert response.status_code == 200
    async with factory() as db:
        stored = await db.get(Order, order_id)
        assert stored.status == "cancelled" and stored.confirmed_at is None
        assert await db.scalar(select(func.count()).select_from(Payment)) == 0
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 1
        )
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        assert (
            inventory.quantity,
            inventory.status,
            inventory.updated_at,
        ) == inventory_before


@pytest.mark.asyncio
async def test_legacy_transition_preserves_delivered_terminal_order(accepted_order):
    _transport, factory, _seeded, order_id = accepted_order
    async with factory() as db:
        order = await db.get(Order, order_id)
        order.status = "delivered"
        await db.commit()

    async with factory() as db:
        with pytest.raises(conversion_service.InvalidOrderTransition):
            await conversion_service.update_order_status(
                db, order_id=order_id, new_status="confirmed"
            )

    async with factory() as db:
        assert (await db.get(Order, order_id)).status == "delivered"
        assert (
            await db.scalar(
                select(func.count())
                .select_from(OperatorAuditEvent)
                .where(OperatorAuditEvent.action == "order.cancelled")
            )
            == 0
        )


@pytest.mark.asyncio
async def test_operator_reports_one_delivery_issue_with_replay_and_no_commerce_mutation(
    accepted_order,
):
    transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        content="Le client signale que le moto ne peut pas livrer.",
        created_at=movement.occurred_at + timedelta(seconds=1),
    )
    async with factory() as db:
        order = await db.get(Order, order_id)
        payment = await db.scalar(select(Payment).where(Payment.order_id == order_id))
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        before = (
            order.status,
            order.updated_at,
            order.delivered_at,
            payment.status,
            payment.amount,
            payment.completed_at,
            inventory.quantity,
            inventory.allocated_quantity,
            inventory.updated_at,
        )
    body = {
        "expected_order_updated_at": reviewed_at.isoformat(),
        "issue_type": "failed",
        "reason_code": "courier_problem",
        "source": "customer_message",
        "summary": "Customer reports that the courier could not complete delivery.",
        "evidence_message_id": str(evidence.message_id),
    }
    first_key, second_key = uuid.uuid4(), uuid.uuid4()
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)

        async def report(key):
            return await client.post(
                f"/api/v1/business/orders/{order_id}/delivery-issues",
                headers=_cancel_headers(csrf, key),
                json=body,
            )

        first, second = await asyncio.gather(report(first_key), report(second_key))
        assert sorted((first.status_code, second.status_code)) == [201, 409]
        created, created_key = (first, first_key) if first.status_code == 201 else (second, second_key)
        assert created.json()["status"] == "open"
        assert created.json()["reported_by_display_name"] == "Commerce.Operator"
        replay = await report(created_key)
        assert replay.status_code == 200
        assert replay.headers["idempotent-replayed"] == "true"
        assert replay.json()["issue_id"] == created.json()["issue_id"]

    async with factory() as db:
        order = await db.get(Order, order_id)
        payment = await db.scalar(select(Payment).where(Payment.order_id == order_id))
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        after = (
            order.status,
            order.updated_at,
            order.delivered_at,
            payment.status,
            payment.amount,
            payment.completed_at,
            inventory.quantity,
            inventory.allocated_quantity,
            inventory.updated_at,
        )
        issues = list(await db.scalars(select(OrderDeliveryIssue)))
        audits = list(
            await db.scalars(
                select(OperatorAuditEvent).where(
                    OperatorAuditEvent.action == "order.delivery_issue.reported"
                )
            )
        )
        issue_dispatch_movement_id = issues[0].dispatch_movement_id
        issue_evidence_message_id = issues[0].evidence_message_id
        with pytest.raises(DBAPIError, match="report evidence is immutable"):
            await db.execute(
                update(OrderDeliveryIssue)
                .where(OrderDeliveryIssue.issue_id == issues[0].issue_id)
                .values(summary="Rewritten report")
            )
            await db.commit()
        await db.rollback()
    assert after == before
    assert len(issues) == 1 and len(audits) == 1
    assert issue_dispatch_movement_id == movement.movement_id
    assert issue_evidence_message_id == evidence.message_id


@pytest.mark.asyncio
async def test_open_issue_blocks_confirmation_until_administrator_closes_it(
    accepted_order,
    monkeypatch: pytest.MonkeyPatch,
):
    transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    evidence = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(seconds=1),
    )
    report_body = {
        "expected_order_updated_at": reviewed_at.isoformat(),
        "issue_type": "failed",
        "reason_code": "recipient_unavailable",
        "source": "courier_report",
        "summary": "Courier reported that the recipient was not available at the address.",
        "evidence_message_id": None,
    }
    async with factory() as db:
        payment = await db.scalar(select(Payment).where(Payment.order_id == order_id))
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        commerce_before = (
            payment.status,
            payment.amount,
            payment.completed_at,
            inventory.quantity,
            inventory.allocated_quantity,
            inventory.updated_at,
        )
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        reported = await client.post(
            f"/api/v1/business/orders/{order_id}/delivery-issues",
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=report_body,
        )
        assert reported.status_code == 201
        issue_id = reported.json()["issue_id"]
        close_url = f"/api/v1/business/orders/{order_id}/delivery-issues/{issue_id}/close"
        close_body = {
            "resolution_code": "delivery_continuing",
            "resolution_reason": "Courier reached the customer and delivery is continuing safely.",
        }
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator_client:
            operator_csrf = await _login(
                operator_client, "commerce.operator", OPERATOR_PASSWORD
            )
            forbidden = await operator_client.post(
                close_url,
                headers=_cancel_headers(operator_csrf, uuid.uuid4()),
                json=close_body,
            )
            assert forbidden.status_code == 403
        future = datetime.now(timezone.utc).timestamp() + 1000
        monkeypatch.setattr("app.api.browser_auth_deps.time.time", lambda: future)
        reauth_required = await client.post(
            close_url,
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json=close_body,
        )
        assert reauth_required.status_code == 403
        csrf = await _reauthenticate(client, csrf)
        blocked = await client.post(
            f"/api/v1/business/orders/{order_id}/confirm-delivery",
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json={
                "expected_updated_at": reviewed_at.isoformat(),
                "evidence_message_id": str(evidence.message_id),
                "attested": True,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["error"]["code"] == "DELIVERY_ISSUE_OPEN"
        close_key = uuid.uuid4()
        closed = await client.post(
            close_url,
            headers=_cancel_headers(csrf, close_key),
            json=close_body,
        )
        assert closed.status_code == 200
        assert closed.json()["status"] == "closed"
        assert closed.json()["resolution_code"] == "delivery_continuing"
        close_replay = await client.post(
            close_url,
            headers=_cancel_headers(csrf, close_key),
            json=close_body,
        )
        assert close_replay.status_code == 200
        assert close_replay.headers["idempotent-replayed"] == "true"
        assert close_replay.json()["issue_id"] == issue_id
        confirmed = await client.post(
            f"/api/v1/business/orders/{order_id}/confirm-delivery",
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json={
                "expected_updated_at": reviewed_at.isoformat(),
                "evidence_message_id": str(evidence.message_id),
                "attested": True,
            },
        )
        assert confirmed.status_code == 200

    async with factory() as db:
        payment = await db.scalar(select(Payment).where(Payment.order_id == order_id))
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        commerce_after = (
            payment.status,
            payment.amount,
            payment.completed_at,
            inventory.quantity,
            inventory.allocated_quantity,
            inventory.updated_at,
        )
        close_audit = await db.scalar(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.action == "order.delivery_issue.closed"
            )
        )
    assert commerce_after == commerce_before
    assert close_audit is not None and close_audit.reason_code == "delivery_continuing"


@pytest.mark.asyncio
async def test_operator_disputes_delivered_order_without_reversing_evidence(
    accepted_order,
):
    transport, factory, seeded, order_id = accepted_order
    reviewed_at, movement = await _make_delivering(factory, order_id)
    receipt = await _receipt_message(
        factory,
        seeded.conversation_id,
        created_at=movement.occurred_at + timedelta(seconds=1),
    )
    delivery = await _confirm_delivery(
        factory,
        order_id=order_id,
        reviewed_at=reviewed_at,
        evidence_message_id=receipt.message_id,
    )
    async with factory() as db:
        payment = await db.scalar(select(Payment).where(Payment.order_id == order_id))
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        before = (
            payment.status,
            payment.amount,
            payment.completed_at,
            inventory.quantity,
            inventory.allocated_quantity,
            inventory.updated_at,
        )
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.operator", OPERATOR_PASSWORD)
        response = await client.post(
            f"/api/v1/business/orders/{order_id}/delivery-issues",
            headers=_cancel_headers(csrf, uuid.uuid4()),
            json={
                "expected_order_updated_at": delivery.order.updated_at.isoformat(),
                "issue_type": "disputed",
                "reason_code": "receipt_disputed",
                "source": "customer_call",
                "summary": "Customer later called and disputed receipt of the delivered Order.",
                "evidence_message_id": str(receipt.message_id),
            },
        )
        assert response.status_code == 201
        detail = await client.get(f"/api/v1/business/orders/{order_id}")
        assert detail.status_code == 200
        assert detail.json()["status"] == "delivered"
        assert detail.json()["delivery"]["issue_state"] == "open"
        assert detail.json()["delivery_issues"][0]["status"] == "open"

    async with factory() as db:
        order = await db.get(Order, order_id)
        confirmation = await db.scalar(
            select(OrderDeliveryConfirmation).where(
                OrderDeliveryConfirmation.order_id == order_id
            )
        )
        issue = await db.scalar(select(OrderDeliveryIssue))
        payment = await db.scalar(select(Payment).where(Payment.order_id == order_id))
        inventory = await db.get(InventoryRecord, seeded.inventory_id)
        after = (
            payment.status,
            payment.amount,
            payment.completed_at,
            inventory.quantity,
            inventory.allocated_quantity,
            inventory.updated_at,
        )
    assert order.status == "delivered" and order.delivered_at == receipt.created_at
    assert confirmation.confirmation_id == issue.delivery_confirmation_id
    assert confirmation.dispatch_movement_id == movement.movement_id
    assert issue.evidence_message_id == receipt.message_id
    assert after == before
