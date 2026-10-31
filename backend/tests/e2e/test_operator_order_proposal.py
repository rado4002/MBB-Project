"""HTTPS browser proof for manual Operator OrderDraft proposal submission."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect, sync_playwright
from sqlalchemy import func, select

from app.database import async_session_factory, engine
from app.models import Customer, Lead, Message, OperatorAccount
from app.models.catalog import SellableItem
from app.models.conversation import Conversation
from app.models.inventory import InventoryRecord
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.stock_movement import StockMovement
from app.tasks import m1
from test_order_drafts_postgres import _block_m1_external_effects
from test_products_stock_inbox import (
    BASE_URL,
    OPERATOR_PASSWORD,
    PRODUCT,
    SKU,
    VARIANT,
    _login,
)


pytestmark = pytest.mark.skipif(
    not os.environ.get("MBB_E2E_BASE_URL"),
    reason="MBB_E2E_BASE_URL requires the isolated E2E runner",
)

CONVERSATION_ID = uuid.UUID("accccccc-cccc-4ccc-8ccc-cccccccccccc")
CUSTOMER_PHONE = "+243812340099"


async def _seed_operator_conversation() -> tuple[int, int, int, int, int, int]:
    now = datetime.now(timezone.utc)
    try:
        async with async_session_factory() as session:
            operator_id = await session.scalar(
                select(OperatorAccount.account_id).where(
                    OperatorAccount.username_normalized == "e2e.operator"
                )
            )
            assert operator_id is not None
            customer = Customer(
                phone_number=CUSTOMER_PHONE,
                name="Proposal Customer",
                city="Kinshasa",
                preferred_language="french",
            )
            conversation = Conversation(
                conversation_id=CONVERSATION_ID,
                customer_id=CUSTOMER_PHONE,
                status="active",
                language_detected="french",
                context={"fixture": "operator-order-proposal-e2e"},
                message_count=1,
                owner_type="human",
                human_owner_account_id=operator_id,
                ai_execution_state="paused",
                ownership_version=1,
                ownership_updated_at=now,
                start_time=now,
                last_message_time=now,
                created_at=now,
                updated_at=now,
            )
            session.add_all([customer, conversation])
            await session.flush()
            session.add_all([
                Message(
                    conversation_id=CONVERSATION_ID,
                    timestamp=now,
                    direction="inbound",
                    content="Je souhaite une unité.",
                    content_type="text",
                    language="french",
                    whatsapp_message_id="proposal-e2e-inbound",
                ),
                Lead(
                    customer_id=CUSTOMER_PHONE,
                    conversation_id=CONVERSATION_ID,
                    score="hot",
                    score_value=9,
                    stage="decision",
                    intent="product_inquiry",
                    product_interest=[SKU],
                    source="e2e",
                    relance_count=0,
                    qualified_at=now,
                    created_at=now,
                    updated_at=now,
                ),
            ])
            await session.commit()
            return (
                await session.scalar(select(func.count()).select_from(Order)),
                await session.scalar(select(func.count()).select_from(Payment)),
                await session.scalar(select(func.count()).select_from(StockMovement)),
                await session.scalar(select(func.count()).select_from(Message)),
                await session.scalar(select(func.count()).select_from(OrderDraft)),
                await session.scalar(
                    select(InventoryRecord.quantity)
                    .join(SellableItem, SellableItem.sellable_item_id == InventoryRecord.sellable_item_id)
                    .where(SellableItem.sku == SKU)
                ),
            )
    finally:
        await engine.dispose()


async def _assert_persisted(before: tuple[int, int, int, int, int, int]) -> tuple[uuid.UUID, int, uuid.UUID, dict]:
    try:
        async with async_session_factory() as session:
            draft = await session.scalar(
                select(OrderDraft).where(OrderDraft.conversation_id == CONVERSATION_ID)
            )
            assert draft is not None
            assert draft.status == "awaiting_confirmation"
            assert draft.draft_version == 1
            assert draft.order_id is None
            assert draft.presented_outbound_message_id is not None
            proposal = await session.get(Message, draft.presented_outbound_message_id)
            assert proposal is not None
            assert proposal.content.endswith(
                "Aucune commande ni aucun paiement n'est encore créé."
            )
            assert proposal.delivery_state == "accepted"
            assert await session.scalar(select(func.count()).select_from(Order)) == before[0]
            assert await session.scalar(select(func.count()).select_from(Payment)) == before[1]
            assert await session.scalar(select(func.count()).select_from(StockMovement)) == before[2]
            assert await session.scalar(select(func.count()).select_from(Message)) == before[3] + 1
            assert await session.scalar(select(func.count()).select_from(OrderDraft)) == before[4] + 1
            item_inventory = await session.scalar(
                select(InventoryRecord)
                .join(SellableItem, SellableItem.sellable_item_id == InventoryRecord.sellable_item_id)
                .where(SellableItem.sku == SKU)
            )
            assert item_inventory is not None and item_inventory.quantity == before[5]
            lead_id = await session.scalar(select(Lead.lead_id).where(Lead.conversation_id == CONVERSATION_ID))
            assert lead_id is not None
            snapshot = {
                "product_id": str(draft.product_id),
                "sellable_item_id": str(draft.sellable_item_id),
                "price_id": str(draft.price_id),
                "exchange_rate_id": str(draft.exchange_rate_id),
                "quantity": str(draft.quantity),
                "unit_price_usd": str(draft.unit_price_usd),
                "unit_price_cdf": str(draft.unit_price_cdf),
            }
            return draft.draft_id, draft.draft_version, lead_id, snapshot
    finally:
        await engine.dispose()


def test_operator_explicitly_reviews_and_submits_order_proposal(monkeypatch: pytest.MonkeyPatch) -> None:
    before = asyncio.run(_seed_operator_conversation())
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    reply_requests: list[tuple[str, int]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True, viewport={"width": 1024, "height": 768})
        page = context.new_page()
        page.on(
            "response",
            lambda response: reply_requests.append((response.url, response.status))
            if response.request.method == "POST" and response.url.endswith("/replies")
            else None,
        )
        _login(page, "e2e.operator", OPERATOR_PASSWORD)
        page.goto(f"{BASE_URL}/business/orders", wait_until="domcontentloaded")
        page.get_by_role("link", name="Create Order").click()
        page.locator(f'a[href="/inbox/{CONVERSATION_ID}"]').click()
        dialog = page.get_by_role("dialog", name="Find a product")
        expect(dialog).to_be_visible()
        dialog.get_by_label("Product name, model or SKU").fill(SKU)
        dialog.get_by_role("button", name="Search").click()
        dialog.get_by_role("button", name=f"{PRODUCT} — {VARIANT}").click()
        quantity = dialog.get_by_label("Quantity")
        quantity.fill("1")
        dialog.get_by_role("button", name="Prepare Order draft").click()
        status = dialog.get_by_role("region", name="Order draft status")
        expect(status.get_by_text("No message was sent.")).to_be_visible(timeout=15_000)
        proposal_text = status.locator("blockquote").inner_text()
        assert re.search(r"Réponds OUI [A-Z0-9]{8}", proposal_text)
        assert reply_requests == []
        status.get_by_role("button", name="Use in reply").click()
        expect(dialog).not_to_be_visible()
        composer = page.get_by_role("textbox", name="Reply to Customer")
        expect(composer).to_have_value(proposal_text)
        expect(composer).to_have_attribute("readonly", "")
        assert reply_requests == []
        send = page.get_by_role("button", name="Send")
        expect(send).to_be_enabled()
        for width in (390, 650, 1024, 1440):
            page.set_viewport_size({"width": width, "height": 768})
            geometry = send.evaluate("""button => {
            const rect = element => {
                const r = element.getBoundingClientRect();
                return { x: r.x, y: r.y, width: r.width, height: r.height };
            };
            const center = rect(button);
            const hit = document.elementFromPoint(center.x + center.width / 2, center.y + center.height / 2);
            return {
                send: center,
                header: rect(document.querySelector('.workspace-header')),
                hit: hit?.className,
                hitTag: hit?.tagName,
                inboxRows: getComputedStyle(document.querySelector('.inbox-page')).gridTemplateRows,
                scroll: [...document.querySelectorAll('.app-main, .inbox-page, .inbox-layout, .conversation-workspace, .workspace-columns, .timeline-panel, .message-history')].map(e => ({className: e.className, clientHeight: e.clientHeight, scrollHeight: e.scrollHeight, overflowY: getComputedStyle(e).overflowY})),
                horizontalOverflow: document.documentElement.scrollWidth > innerWidth,
            };
        }""")
            print(f"Composer geometry at {width}px: {geometry}")
            assert geometry["send"]["y"] >= geometry["header"]["y"] + geometry["header"]["height"]
            assert geometry["send"]["y"] + geometry["send"]["height"] <= 768
            assert geometry["hitTag"] == "BUTTON"
            assert not geometry["horizontalOverflow"]
        send.focus()
        expect(send).to_be_focused()
        page.keyboard.press("Tab")
        page.keyboard.press("Shift+Tab")
        expect(send).to_be_focused()
        page.set_viewport_size({"width": 1024, "height": 768})
        send.click()
        expect(page.get_by_text("Pending delivery")).to_be_visible(timeout=15_000)
        assert len(reply_requests) == 1 and reply_requests[0][1] == 202
        context.close()
        browser.close()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    draft_id, draft_version, lead_id, snapshot = asyncio.run(_assert_persisted(before))
    external_calls = _block_m1_external_effects(
        monkeypatch, async_session_factory, whatsapp_send_enabled=False,
    )

    class TaskStub:
        request = SimpleNamespace(retries=0)

        def retry(self, **_kwargs):
            raise AssertionError("M1 unexpectedly requested a Celery retry")

    async def confirm(message_id: uuid.UUID, wa_id: str) -> dict:
        return await m1._process(
            task=TaskStub(), message_id=str(message_id),
            customer_phone=CUSTOMER_PHONE, content=f"OUI {confirmation_code}",
            content_type="text", timestamp=datetime.now(timezone.utc).isoformat(),
            whatsapp_message_id=wa_id,
        )

    async def check_confirmed(order_id: uuid.UUID) -> None:
        async with async_session_factory() as session:
                orders = list((await session.scalars(select(Order))).all())
                assert len(orders) == before[0] + 1
                order = next(order for order in orders if order.order_id == order_id)
                assert order.order_id == order_id
                assert order.status == "pending"
                assert order.customer_id == CUSTOMER_PHONE and order.lead_id == lead_id
                assert order.currency == "CDF" and order.total_amount == draft_total
                assert len(order.items) == 1
                line = order.items[0]
                for key, value in snapshot.items():
                    assert str(line[key]) == value
                draft = await session.get(OrderDraft, (draft_id, draft_version))
                assert draft is not None and draft.status == "confirmed"
                assert draft.order_id == order_id and draft.draft_version == 1
                assert await session.scalar(select(func.count()).select_from(OrderDraft)) == before[4] + 1
                assert await session.scalar(select(func.count()).select_from(Payment)) == before[1]
                assert await session.scalar(select(func.count()).select_from(StockMovement)) == before[2]

    async def load_confirmation() -> tuple[str, Decimal]:
        async with async_session_factory() as session:
                draft = await session.get(OrderDraft, (draft_id, draft_version))
                assert draft is not None
                return draft.confirmation_code, draft.total_cdf

    async def connected_confirmation() -> uuid.UUID:
        nonlocal confirmation_code, draft_total
        try:
            confirmation_code, draft_total = await load_confirmation()
            first = await confirm(uuid.uuid4(), "guided-order-confirm-first")
            assert first["status"] == "order_draft_confirmed"
            assert first["send_status"] == "skipped"
            order_id = uuid.UUID(first["order_id"])
            await check_confirmed(order_id)
            replay = await confirm(uuid.uuid4(), "guided-order-confirm-replay")
            assert replay["status"] == "order_draft_already_confirmed"
            assert replay["order_id"] == first["order_id"]
            assert replay["send_status"] == "skipped"
            await check_confirmed(order_id)
            assert external_calls == []
            return order_id
        finally:
            await engine.dispose()

    confirmation_code = ""
    draft_total = Decimal(0)
    order_id = asyncio.run(connected_confirmation())

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        _login(page, "e2e.operator", OPERATOR_PASSWORD)
        listing = page.request.get(f"{BASE_URL}/api/v1/business/orders?limit=20&offset=0&status=pending")
        assert listing.status == 200
        assert any(item["order_id"] == str(order_id) for item in listing.json()["items"])
        detail = page.request.get(f"{BASE_URL}/api/v1/business/orders/{order_id}")
        assert detail.status == 200
        assert detail.json()["order_id"] == str(order_id)
        assert detail.json()["status"] == "pending"
        page.goto(f"{BASE_URL}/business/orders?status=pending&offset=0")
        order_link = page.locator(f'a[data-order-id="{order_id}"]')
        expect(order_link).to_be_visible()
        order_link.click()
        expect(page.get_by_role("region", name="Order detail")).to_contain_text("Pending")
        context.close()
        browser.close()
