"""Real UI -> API -> domain -> PostgreSQL -> UI commerce regression."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, expect, sync_playwright
import pytest
from sqlalchemy import func, select, text

from app.database import async_session_factory, engine
from app.ai.provider_contract import ProviderFinishReason, ProviderToolCall, ProviderTurnResult
from app.ai.turn import AITurn, AITurnService
from app.models import (
    Conversation,
    Customer,
    ExchangeRate,
    Lead,
    Message,
    OperatorAccount,
)
from app.models.catalog import SellableItem
from app.models.inventory import InventoryRecord
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.stock_movement import StockMovement
from app.modules.m1_gateway.turn_recovery import add_pending_turn, claim_turn, mark_outcome_committed
from app.modules.product_offer.service import get_product_offer
from app.operator_identity.passwords import hash_password
from app.tasks import m1
from test_order_drafts_postgres import _block_m1_external_effects


BASE_URL = os.environ.get("MBB_E2E_BASE_URL")
pytestmark = pytest.mark.skipif(
    not BASE_URL, reason="MBB_E2E_BASE_URL requires the isolated E2E runner"
)

ADMIN_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OPERATOR_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
CONVERSATION_ID = uuid.UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")
ADMIN_PASSWORD = "Violet-River-7329!"
OPERATOR_PASSWORD = "Amber-Cloud-5187!"
PRODUCT = "E2E Solar Kettle"
VARIANT = "Three litre"
SKU = "E2E-SOLAR-KETTLE-3L"
ORDER_SOURCE_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
FIRST_CONFIRMATION_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
REPLAY_CONFIRMATION_ID = uuid.UUID("ffffffff-ffff-4fff-8fff-ffffffffffff")


async def _seed_prerequisites() -> None:
    now = datetime.now(UTC)
    admin = OperatorAccount(
        account_id=ADMIN_ID,
        username_normalized="e2e.admin",
        display_name="E2E Administrator",
        password_hash=hash_password(ADMIN_PASSWORD),
        role="administrator",
        status="active",
        must_change_password=False,
        password_changed_at=now,
    )
    operator = OperatorAccount(
        account_id=OPERATOR_ID,
        username_normalized="e2e.operator",
        display_name="E2E Operator",
        password_hash=hash_password(OPERATOR_PASSWORD),
        role="operator",
        status="active",
        must_change_password=False,
        password_changed_at=now,
    )
    customer = Customer(
        phone_number="+243812345678",
        name="E2E Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=CONVERSATION_ID,
        customer_id=customer.phone_number,
        status="active",
        language_detected="french",
        context={"fixture": "products-stock-inbox-e2e"},
        message_count=1,
        owner_type="ai",
        ai_execution_state="eligible",
    )
    try:
        async with async_session_factory() as session:
            session.add_all([admin, operator, customer])
            await session.flush()
            session.add(conversation)
            await session.flush()
            session.add_all(
                [
                    Message(
                        conversation_id=CONVERSATION_ID,
                        direction="inbound",
                        content="Je cherche le prix et la disponibilité du produit de test.",
                        content_type="text",
                        language="french",
                        whatsapp_message_id="e2e-inbound-1",
                    ),
                    Lead(
                        customer_id=customer.phone_number,
                        conversation_id=CONVERSATION_ID,
                        score="warm",
                        score_value=5,
                        stage="consideration",
                        intent="product_inquiry",
                        product_interest=[],
                        source="e2e",
                    ),
                    ExchangeRate(
                        base_currency="USD",
                        quote_currency="CDF",
                        rate=Decimal("2800.000000"),
                        authority_mode="MANUAL",
                        source="MBB_ADMIN",
                        validation_status="ADMIN_APPROVED",
                    ),
                ]
            )
            await session.commit()
    finally:
        await engine.dispose()


async def _database_state(sku: str) -> dict:
    statement = text(
        """
        SELECT p.active AS product_active, s.active AS variant_active,
               pr.amount AS usd_price, pr.currency,
               i.quantity, i.status AS inventory_status,
               (SELECT count(*) FROM mbb.sellable_item_prices current_price
                WHERE current_price.sellable_item_id = s.sellable_item_id
                  AND current_price.ended_at IS NULL) AS current_price_rows,
               (SELECT json_agg(json_build_object(
                    'kind', movement.operation_kind,
                    'before', movement.before_quantity,
                    'after', movement.after_quantity,
                    'received', movement.received_amount,
                    'corrected', movement.corrected_quantity,
                    'reason', movement.reason
                ) ORDER BY movement.occurred_at, movement.movement_id)
                FROM mbb.stock_movements movement
                WHERE movement.sellable_item_id = s.sellable_item_id) AS movements,
               (SELECT count(*) FROM mbb.orders) AS order_count,
               (SELECT count(*) FROM mbb.payments) AS payment_count
        FROM mbb.products p
        JOIN mbb.sellable_items s ON s.product_id = p.product_id
        JOIN mbb.sellable_item_prices pr
          ON pr.sellable_item_id = s.sellable_item_id AND pr.ended_at IS NULL
        JOIN mbb.inventory_statuses i ON i.sellable_item_id = s.sellable_item_id
        WHERE s.sku = :sku
        """
    )
    try:
        async with async_session_factory() as session:
            row = (await session.execute(statement, {"sku": sku})).mappings().one()
            return dict(row)
    finally:
        await engine.dispose()


def _login(page: Page, username: str, password: str) -> None:
    page.goto(f"{BASE_URL}/login", wait_until="networkidle")
    page.get_by_label("Username").fill(username)
    page.locator("#password").fill(password)
    page.get_by_role("button", name="Sign in").click()
    expect(page).to_have_url(re.compile(r"/inbox(?:/|$)"), timeout=15_000)


def _stock_change(page: Page, kind: str, value: int, reason: str | None = None) -> None:
    page.get_by_role("button", name=kind).click()
    field = "Amount received" if kind == "Receive stock" else "Corrected quantity"
    page.get_by_label(field).fill(str(value))
    if reason is not None:
        page.get_by_label("Reason for correction").fill(reason)
    page.get_by_role("button", name="Review change").click()
    expect(page.get_by_role("region", name="Confirm stock change")).to_be_visible()
    page.get_by_role("button", name="Confirm stock change").click()
    expect(page.get_by_text("Stock change saved. Showing current stock.")).to_be_visible(
        timeout=15_000
    )


def _open_offer(page: Page):
    page.goto(f"{BASE_URL}/inbox/{CONVERSATION_ID}", wait_until="domcontentloaded")
    trigger = page.get_by_role("button", name="Find a product")
    expect(trigger).to_be_visible(timeout=15_000)
    trigger.click()
    dialog = page.get_by_role("dialog", name="Find a product")
    dialog.get_by_label("Product name, model or SKU").fill(SKU)
    dialog.get_by_role("button", name="Search").click()
    result = dialog.get_by_role("button", name=f"{PRODUCT} — {VARIANT}")
    expect(result).to_be_visible(timeout=15_000)
    result.click()
    expect(dialog.get_by_role("heading", name=f"{PRODUCT} — {VARIANT}")).to_be_visible(
        timeout=15_000
    )
    return dialog


async def _assert_order_acceptance(monkeypatch: pytest.MonkeyPatch) -> uuid.UUID:
    """Continue the browser-created offer through scripted AI and real M1."""
    factory = async_session_factory
    try:
        async with factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == "d7e8f9a0b1c2"
            item = await session.scalar(select(SellableItem).where(SellableItem.sku == SKU))
            conversation = await session.get(Conversation, CONVERSATION_ID)
            lead = await session.scalar(select(Lead).where(Lead.conversation_id == CONVERSATION_ID))
            assert item is not None and conversation is not None and lead is not None
            assert conversation.customer_id == "+243812345678"
            assert conversation.owner_type == "ai" and conversation.ai_execution_state == "eligible"
            offer = await get_product_offer(session, item.sellable_item_id)
            inventory = await session.scalar(
                select(InventoryRecord).where(InventoryRecord.sellable_item_id == item.sellable_item_id)
            )
            assert offer is not None and inventory is not None
            assert offer.product_name == PRODUCT and offer.current_usd_price == Decimal("55.25")
            assert offer.inventory_status == "available" and offer.is_sellable_now
            quote = offer.derived_cdf_quote
            assert quote is not None
            assert quote.usd_to_cdf_rate == Decimal("2800")
            assert quote.cdf_amount == Decimal("154700.00")
            assert inventory.quantity == 3
            inventory_before = (inventory.quantity, inventory.status, inventory.updated_at)
            movement_count = await session.scalar(
                select(func.count()).select_from(StockMovement).where(
                    StockMovement.sellable_item_id == item.sellable_item_id
                )
            )
            assert movement_count == 3
            assert await session.scalar(select(func.count()).select_from(Order)) == 0
            assert await session.scalar(select(func.count()).select_from(Payment)) == 0

            # The browser fixture begins at inquiry; qualify it for the scripted
            # draft turn without replacing any Product, Pricing, FX, or Stock row.
            lead.score = "hot"
            lead.score_value = 9
            lead.stage = "decision"
            session.add(Message(
                message_id=ORDER_SOURCE_ID,
                conversation_id=CONVERSATION_ID,
                timestamp=datetime.now(UTC),
                direction="inbound",
                content="Je le prends.",
                content_type="text",
                language="french",
                whatsapp_message_id="e2e-order-source",
            ))
            await session.flush()
            add_pending_turn(
                session,
                source_message_id=ORDER_SOURCE_ID,
                conversation_id=CONVERSATION_ID,
                ownership_version=conversation.ownership_version,
            )
            await session.commit()

        # Reuse the M1 tests' fail-closed provider, transport, CRM, payment,
        # inventory-adapter, FX, and Celery guards. Enable the send gate to
        # prove Order acceptance itself still takes the no-send path.
        external_calls = _block_m1_external_effects(
            monkeypatch, factory, whatsapp_send_enabled=True,
        )
        assert (await claim_turn(ORDER_SOURCE_ID, attempt_id="e2e-draft")).action == "process"

        class ScriptedDraftAdapter:
            calls = 0

            async def generate_turn(self, _request):
                self.calls += 1
                return ProviderTurnResult(
                    tool_calls=(ProviderToolCall(
                        call_id="e2e-order-draft",
                        capability_name="prepare_order_draft",
                        arguments={
                            "selected_sellable_item_id": str(item.sellable_item_id),
                            "quantity": 2,
                        },
                    ),),
                    finish_reason=ProviderFinishReason.tool_call,
                )

        async def current_authority(_context):
            return True

        async def load_state(_conversation_id):
            return None

        adapter = ScriptedDraftAdapter()
        service = AITurnService(
            adapter,
            authority_checker=current_authority,
            durable_session_factory=factory,
            commercial_state_loader=load_state,
            turn_lifecycle_recorder=mark_outcome_committed,
        )
        presented = await service.generate_finalized(AITurn(
            user_content="Je le prends.",
            language="french",
            expected_ownership_version=conversation.ownership_version,
            conversation_id=CONVERSATION_ID,
            source_message_id=ORDER_SOURCE_ID,
            allowed_capabilities=("prepare_order_draft",),
        ))
        assert adapter.calls == 1
        assert presented.audit_persisted
        assert presented.audit_record.outcome.value == "order_draft_presented"
        assert presented.text is not None and "309 400 CDF" in presented.text

        async with factory() as session:
            draft = await session.scalar(select(OrderDraft).where(
                OrderDraft.conversation_id == CONVERSATION_ID
            ))
            assert draft is not None and draft.status == "awaiting_confirmation"
            assert draft.source_message_id == ORDER_SOURCE_ID and draft.quantity == 2
            assert draft.product_id == offer.product_id
            assert draft.sellable_item_id == offer.sellable_item_id
            assert draft.price_id == offer.price_id
            assert draft.exchange_rate_id == quote.exchange_rate_id
            assert draft.unit_price_usd == Decimal("55.25")
            assert draft.unit_price_cdf == Decimal("154700.00")
            assert draft.total_cdf == Decimal("309400.00")
            assert draft.inventory_status == inventory_before[1]
            assert draft.inventory_updated_at == inventory_before[2]
            confirmation = f"OUI {draft.confirmation_code}"

        class TaskStub:
            request = SimpleNamespace(retries=0)

            def retry(self, **_kwargs):
                raise AssertionError("M1 unexpectedly retried")

        async def confirm(message_id: uuid.UUID, whatsapp_message_id: str):
            return await m1._process(
                task=TaskStub(),
                message_id=str(message_id),
                customer_phone="+243812345678",
                content=confirmation,
                content_type="text",
                timestamp=datetime.now(UTC).isoformat(),
                whatsapp_message_id=whatsapp_message_id,
            )

        first = await confirm(FIRST_CONFIRMATION_ID, "e2e-order-confirm-first")
        replay = await confirm(REPLAY_CONFIRMATION_ID, "e2e-order-confirm-replay")
        redelivery = await confirm(FIRST_CONFIRMATION_ID, "e2e-order-confirm-first")
        assert first["status"] == "order_draft_confirmed"
        assert replay["status"] == "order_draft_already_confirmed"
        assert first["order_id"] == replay["order_id"]
        assert first["send_status"] == replay["send_status"] == "skipped"
        assert redelivery["status"] == "skipped"
        assert redelivery["send_status"] == "skipped"

        async with factory() as session:
            orders = list((await session.scalars(select(Order))).all())
            assert len(orders) == 1
            order = orders[0]
            assert order.order_id == uuid.UUID(first["order_id"])
            assert order.status == "pending" and order.total_amount == Decimal("309400.00")
            assert order.customer_id == "+243812345678" and order.lead_id == lead.lead_id
            assert order.confirmed_at is None and order.delivered_at is None
            assert order.hub_crm_synced is False and order.hub_crm_order_id is None
            assert len(order.items) == 1
            line = order.items[0]
            assert line["product_id"] == str(offer.product_id)
            assert line["sellable_item_id"] == str(offer.sellable_item_id)
            assert line["price_id"] == str(offer.price_id)
            assert line["exchange_rate_id"] == str(quote.exchange_rate_id)
            assert line["quantity"] == 2
            assert Decimal(line["unit_price_usd"]) == Decimal("55.25")
            assert Decimal(line["usd_to_cdf_rate"]) == Decimal("2800")
            assert Decimal(line["unit_price_cdf"]) == Decimal("154700.00")
            stored_draft = await session.get(OrderDraft, (draft.draft_id, draft.draft_version))
            assert stored_draft is not None and stored_draft.status == "confirmed"
            assert stored_draft.order_id == order.order_id
            inventory_after = await session.get(InventoryRecord, inventory.inventory_id)
            assert inventory_after is not None
            assert (inventory_after.quantity, inventory_after.status, inventory_after.updated_at) == inventory_before
            assert await session.scalar(
                select(func.count()).select_from(StockMovement).where(
                    StockMovement.sellable_item_id == item.sellable_item_id
                )
            ) == movement_count
            assert await session.scalar(select(func.count()).select_from(Payment)) == 0
            assert await session.scalar(
                select(func.count()).select_from(Message).where(
                    Message.whatsapp_message_id == "e2e-order-confirm-first"
                )
            ) == 1
            receipt_orders = dict((await session.execute(
                select(Message.message_id, Message.inbound_receipt_order).where(Message.message_id.in_((
                    ORDER_SOURCE_ID, FIRST_CONFIRMATION_ID, REPLAY_CONFIRMATION_ID,
                )))
            )).all())
            assert len(receipt_orders) == 3
            assert (receipt_orders[ORDER_SOURCE_ID]
                    < receipt_orders[FIRST_CONFIRMATION_ID]
                    < receipt_orders[REPLAY_CONFIRMATION_ID])
        assert external_calls == []
        return order.order_id
    finally:
        await engine.dispose()


def test_products_stock_inbox_complete_journey(monkeypatch: pytest.MonkeyPatch) -> None:
    asyncio.run(_seed_prerequisites())
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    responses: list[tuple[str, str, int]] = []
    with sync_playwright() as playwright:
        def run_browser() -> None:
            browser = playwright.chromium.launch(headless=True)
            admin_context = browser.new_context(ignore_https_errors=True)
            page = admin_context.new_page()
            page.on(
                "response",
                lambda response: responses.append(
                    (response.request.method, urlparse(response.url).path, response.status)
                )
                if "/api/v1/" in response.url
                else None,
            )
            _login(page, "e2e.admin", ADMIN_PASSWORD)

            page.goto(f"{BASE_URL}/business/stock", wait_until="domcontentloaded")
            page.get_by_role("link", name="Add new product").click()
            page.get_by_label("Product name", exact=True).fill(PRODUCT)
            page.get_by_label("Category code").fill("integration_appliance")
            page.get_by_label("Description").fill(
                "Disposable Product, Stock, and Inbox regression fixture."
            )
            page.get_by_role("button", name="Save product").click()
            expect(page.get_by_role("heading", name="Add first variant")).to_be_visible(
                timeout=15_000
            )
            page.get_by_label("Variant name (optional)").fill(VARIANT)
            page.get_by_label("SKU (optional)").fill(SKU)
            page.get_by_role("button", name="Save first variant").click()
            expect(page).to_have_url(
                re.compile(r"/business/stock\?.*item="), timeout=15_000
            )
            item_id = parse_qs(urlparse(page.url).query)["item"][0]
            expect(page.get_by_role("heading", name=f"{PRODUCT} · {VARIANT}")).to_be_visible(
                timeout=15_000
            )
            initial = page.locator(".stock-detail").inner_text()
            assert "Inactive" in initial
            assert "Current USD price\nNot set" in initial
            assert "Current quantity\nUnknown" in initial
            assert page.get_by_role("button", name="Receive stock").is_disabled()

            page.get_by_role("button", name="Set price").click()
            page.get_by_label("New USD price").fill("55.25")
            page.get_by_role("button", name="Save price").click()
            expect(page.get_by_text("Price saved. Showing current facts.")).to_be_visible(
                timeout=15_000
            )
            expect(page.get_by_role("heading", name="Adjust stock")).to_be_visible()
            page.get_by_label("Corrected quantity").fill("0")
            page.get_by_label("Reason for correction").fill("Initial verified count")
            page.get_by_role("button", name="Review change").click()
            page.get_by_role("button", name="Confirm stock change").click()
            expect(page.get_by_text("Stock change saved. Showing current stock.")).to_be_visible(
                timeout=15_000
            )
            zero_stock = page.locator(".stock-detail").inner_text()
            assert "Current quantity\n0" in zero_stock
            assert "Out of stock" in zero_stock
            product_href = page.get_by_role("link", name="View product").get_attribute("href")
            assert product_href

            page.goto(f"{BASE_URL}/inbox/{CONVERSATION_ID}", wait_until="domcontentloaded")
            page.get_by_role("button", name="Find a product").click()
            inactive_dialog = page.get_by_role("dialog", name="Find a product")
            inactive_dialog.get_by_label("Product name, model or SKU").fill(SKU)
            inactive_dialog.get_by_role("button", name="Search").click()
            expect(
                inactive_dialog.get_by_text(
                    "No matching active variants. Try another name, model or SKU."
                )
            ).to_be_visible(timeout=15_000)
            inactive_dialog.get_by_role("button", name="Back to conversation").click()

            page.goto(f"{BASE_URL}{product_href}", wait_until="domcontentloaded")
            page.get_by_role("button", name=f"{VARIANT} · {SKU}").click()
            page.get_by_role("button", name="Activate variant").click()
            page.get_by_role("button", name="Confirm activate variant").click()
            expect(page.get_by_text("Active variant")).to_be_visible(timeout=15_000)
            page.wait_for_load_state("networkidle")
            page.get_by_role("button", name="Activate product").click()
            expect(page.get_by_role("button", name="Confirm activate product")).to_be_visible(
                timeout=15_000
            )
            page.get_by_role("button", name="Confirm activate product").click()
            expect(page.get_by_text("Active product")).to_be_visible(timeout=15_000)

            unavailable = _open_offer(page).inner_text()
            assert "Current price\nUSD 55.25" in unavailable
            assert "CDF quote\nCDF 154700.00" in unavailable
            assert "Availability\nUnavailable" in unavailable
            assert "Commercial status\nUnavailable" in unavailable
            assert "Reason\nMarked unavailable" in unavailable
            page.get_by_role("dialog", name="Find a product").get_by_role(
                "button", name="Back to conversation"
            ).click()

            page.goto(f"{BASE_URL}/business/stock?item={item_id}", wait_until="domcontentloaded")
            expect(page.get_by_role("heading", name=f"{PRODUCT} · {VARIANT}")).to_be_visible(
                timeout=15_000
            )
            _stock_change(page, "Receive stock", 5)
            five = page.locator(".stock-detail").inner_text()
            assert "Current quantity\n5" in five and "Available" in five
            stale = page.evaluate(
                """async (id) => {
                  const response = await fetch(`/api/v1/business/stock?limit=50&item_id=${encodeURIComponent(id)}`);
                  const item = (await response.json()).items[0];
                  return {quantity: item.quantity, updated_at: item.inventory_updated_at};
                }""",
                item_id,
            )
            assert stale["quantity"] == 5 and stale["updated_at"]

            _stock_change(page, "Adjust stock", 3, "Cycle count correction")
            final_stock = page.locator(".stock-detail").inner_text()
            assert "Current quantity\n3" in final_stock
            assert "Unknown → 0" in final_stock
            assert "0 → 5" in final_stock
            assert "5 → 3" in final_stock

            stale_result = page.evaluate(
                """async ({id, stale}) => {
                  const csrf = await (await fetch('/api/v1/auth/csrf')).json();
                  const response = await fetch(`/api/v1/operator/commerce/sellable-items/${encodeURIComponent(id)}/inventory/adjust`, {
                    method: 'POST', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token},
                    body: JSON.stringify({corrected_quantity: 9, reason: 'stale overwrite attempt',
                      expected_quantity: stale.quantity, expected_updated_at: stale.updated_at,
                      operation_key: crypto.randomUUID()})
                  });
                  return {status: response.status, body: await response.json()};
                }""",
                {"id": item_id, "stale": stale},
            )
            assert stale_result["status"] == 409
            assert stale_result["body"]["error"]["code"] == "STOCK_CONFLICT"

            available = _open_offer(page).inner_text()
            assert "Current price\nUSD 55.25" in available
            assert "CDF quote\nCDF 154700.00" in available
            assert "Availability\nAvailable" in available
            assert "Commercial status\nSellable now" in available
            assert "Reason\nActive, priced and marked available" in available

            operator_context = browser.new_context(ignore_https_errors=True)
            operator_page = operator_context.new_page()
            _login(operator_page, "e2e.operator", OPERATOR_PASSWORD)
            operator_page.goto(
                f"{BASE_URL}/business/stock?item={item_id}", wait_until="domcontentloaded"
            )
            expect(
                operator_page.get_by_role("heading", name=f"{PRODUCT} · {VARIANT}")
            ).to_be_visible(timeout=15_000)
            operator_stock = operator_page.locator(".stock-detail").inner_text()
            assert "Availability\nAvailable" in operator_stock
            assert "Current quantity" not in operator_stock
            assert "Current USD price" not in operator_stock
            assert not operator_page.get_by_role("button", name="Adjust stock").is_visible()
            forbidden = operator_page.evaluate(
                """async (id) => {
                  const csrf = await (await fetch('/api/v1/auth/csrf')).json();
                  const response = await fetch(`/api/v1/operator/commerce/sellable-items/${encodeURIComponent(id)}/price`, {
                    method: 'PUT', credentials: 'same-origin',
                    headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf.csrf_token},
                    body: JSON.stringify({amount: '99.99', currency: 'USD'})
                  });
                  return {status: response.status, body: await response.json()};
                }""",
                item_id,
            )
            assert forbidden["status"] == 403
            assert forbidden["body"]["error"]["code"] == "FORBIDDEN"
            operator_context.close()
            admin_context.close()
            browser.close()

        run_browser()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    state = asyncio.run(_database_state(SKU))
    assert state["product_active"] is True
    assert state["variant_active"] is True
    assert state["usd_price"] == Decimal("55.25")
    assert state["currency"] == "USD"
    assert state["current_price_rows"] == 1
    assert state["quantity"] == 3
    assert state["inventory_status"] == "available"
    assert state["movements"] == [
        {
            "kind": "adjust", "before": None, "after": 0,
            "received": None, "corrected": 0, "reason": "Initial verified count",
        },
        {
            "kind": "receive", "before": 0, "after": 5,
            "received": 5, "corrected": None, "reason": None,
        },
        {
            "kind": "adjust", "before": 5, "after": 3,
            "received": None, "corrected": 3, "reason": "Cycle count correction",
        },
    ]
    assert state["order_count"] == 0
    assert state["payment_count"] == 0

    assert ("POST", "/api/v1/operator/commerce/products", 201) in responses
    assert any(method == "PUT" and path.endswith("/price") and status == 200 for method, path, status in responses)
    assert any(method == "POST" and path.endswith("/inventory/receive") and status == 200 for method, path, status in responses)
    assert any(method == "POST" and path.endswith("/inventory/adjust") and status == 409 for method, path, status in responses)
    assert any(method == "GET" and path == "/api/v1/operator/product-offers" and status == 200 for method, path, status in responses)
    order_id = asyncio.run(_assert_order_acceptance(monkeypatch))
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 390, "height": 844})
        page = context.new_page()
        _login(page, "e2e.operator", OPERATOR_PASSWORD)
        page.goto(f"{BASE_URL}/business/stock", wait_until="domcontentloaded")
        page.get_by_label("Business", exact=True).select_option("orders")
        expect(page).to_have_url(re.compile(r"/business/orders$"))
        expect(page.get_by_role("heading", name="Orders", exact=True)).to_be_visible()
        expect(page.get_by_role("link", name=re.compile(PRODUCT))).to_be_visible(timeout=15_000)
        page.get_by_role("link", name=re.compile(PRODUCT)).click()
        expect(page).to_have_url(re.compile(rf"/business/orders/{order_id}"))
        detail = page.get_by_role("region", name="Order detail")
        expect(detail.get_by_text("pending", exact=True)).to_be_visible(timeout=15_000)
        expect(detail.get_by_text("CDF 309,400.00")).to_be_visible()
        expect(detail.get_by_text("USD 55.25")).to_be_visible()
        expect(detail.get_by_text("Accepted USD/CDF rate 2800")).to_be_visible()
        expect(detail.get_by_role("link", name="Open associated Inbox conversation")).to_have_attribute(
            "href", f"/inbox/{CONVERSATION_ID}"
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not detail.get_by_role("button").count()
        context.close()
        browser.close()
