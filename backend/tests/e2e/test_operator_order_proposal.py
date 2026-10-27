"""HTTPS browser proof for manual Operator OrderDraft proposal submission."""

from __future__ import annotations

import asyncio
import os
import re
import sys
import uuid
from datetime import datetime, timezone

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


async def _assert_persisted(before: tuple[int, int, int, int, int, int]) -> None:
    try:
        async with async_session_factory() as session:
            draft = await session.scalar(
                select(OrderDraft).where(OrderDraft.conversation_id == CONVERSATION_ID)
            )
            assert draft is not None
            assert draft.status == "awaiting_confirmation"
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
    finally:
        await engine.dispose()


def test_operator_explicitly_reviews_and_submits_order_proposal() -> None:
    before = asyncio.run(_seed_operator_conversation())
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    reply_requests: list[tuple[str, int]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        page.on(
            "response",
            lambda response: reply_requests.append((response.url, response.status))
            if response.request.method == "POST" and response.url.endswith("/replies")
            else None,
        )
        _login(page, "e2e.operator", OPERATOR_PASSWORD)
        page.goto(f"{BASE_URL}/inbox/{CONVERSATION_ID}", wait_until="domcontentloaded")
        page.get_by_role("button", name="Find a product").click()
        dialog = page.get_by_role("dialog", name="Find a product")
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
        page.get_by_role("button", name="Send").click()
        expect(page.get_by_text("Pending delivery")).to_be_visible(timeout=15_000)
        assert len(reply_requests) == 1 and reply_requests[0][1] == 202
        context.close()
        browser.close()

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(_assert_persisted(before))
