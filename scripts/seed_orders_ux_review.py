"""Seed fictional Orders in the launcher-owned disposable PostgreSQL only."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.database import async_session_factory, engine
from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.inventory import InventoryRecord
from app.models.lead import Lead
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.models.payment import Payment
from app.modules.m7_conversion.order_allocation import allocate_paid_order_stock
from app.modules.m7_conversion.order_dispatch import dispatch_prepared_order
from app.modules.m7_conversion.order_fulfillment import prepare_confirmed_order

NAMESPACE = uuid.UUID("6804b300-7acc-41c8-a5c4-c151ed017140")
NAMES = (
    "Solar starter kit 120 W", "Smart lock with keypad and backup key",
    "Power bank 20,000 mAh", "Rechargeable LED study lamp", "USB-C charging cable 2 m",
    "Solar water pump for garden irrigation", "Portable solar panel 80 W",
    "Motion sensor security light", "Compact inverter 300 W",
    "Long-name solar home lighting kit with three lamps and mobile charging ports",
    "Wireless door sensor", "Rechargeable work light", "Surge protected power strip",
    "Solar lantern with radio", "Smart plug with timer", "Water pump controller",
    "Foldable solar charging mat",
)
STATES = ("pending",) * 5 + ("confirmed",) * 5 + ("preparing",) * 3 + ("delivering",) * 2 + ("cancelled",) * 2
ZONES = ("Gombe", "Limete", "Kintambo", "Ngaliema", "Masina", "Lemba", "Matete")


def identity(kind: str, index: int) -> uuid.UUID:
    return uuid.uuid5(NAMESPACE, f"{kind}-{index}")


async def seed() -> None:
    if os.environ.get("MBB_ORDERS_UX_OWNED") != "true" or os.environ.get("POSTGRES_USER") != "mbb_ux":
        raise RuntimeError("Only the verified Orders UX launcher may seed this dataset")
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        if await session.scalar(select(func.count()).select_from(Order)) != 0:
            raise RuntimeError("Disposable review database is not empty; refusing duplicate seed")
        administrator = await session.scalar(select(OperatorAccount).where(OperatorAccount.username_normalized == "uxreview.administrator"))
        if administrator is None:
            raise RuntimeError("Dedicated review administrator is missing")
        for index, (name, state) in enumerate(zip(NAMES, STATES, strict=True)):
            customer_id = f"+243899{index:09d}"
            customer = Customer(phone_number=customer_id, name=f"Fictional Review Customer {index + 1}", city="Kinshasa", preferred_language="french")
            conversation_id = identity("conversation", index)
            conversation = Conversation(conversation_id=conversation_id, customer_id=customer_id, status="active", language_detected="french", context={"fixture": "fictional-orders-ux-review"}, owner_type="ai", ai_execution_state="eligible")
            lead = Lead(lead_id=identity("lead", index), customer_id=customer_id, conversation_id=conversation_id, score="warm", score_value=5, stage="decision", intent="product_inquiry", product_interest=[name], source="fictional_ux_review", relance_count=0)
            product = Product(product_id=identity("product", index), name=name, category_code="review_fixture", description=f"Fictional UX review product: {name}.", active=True)
            item = SellableItem(sellable_item_id=identity("item", index), product_id=product.product_id, model_label=f"Review model {index + 1}", sku=f"UX-ORDER-{index + 1:03d}", attributes={}, active=True)
            inventory = InventoryRecord(inventory_id=identity("inventory", index), sellable_item_id=item.sellable_item_id, status="available", quantity=30, updated_at=now)
            session.add_all([customer, product])
            await session.flush()
            session.add_all([conversation, item])
            await session.flush()
            session.add_all([lead, inventory])
            await session.flush()
            quantity = 1 + index % 3
            unit = Decimal(18000 + index * 4250)
            total = unit * quantity
            created = now - timedelta(days=17 - index, hours=index % 5)
            order = Order(order_id=identity("order", index), lead_id=lead.lead_id, customer_id=customer_id,
                          items=[{"product_id": str(product.product_id), "sellable_item_id": str(item.sellable_item_id), "product_name": name, "model_label": item.model_label, "quantity": quantity, "unit_price_cdf": str(unit)}],
                          total_amount=total, currency="CDF", payment_type="cod", delivery_zone=ZONES[index % len(ZONES)], delivery_method="moto_taxi", status="cancelled" if state == "cancelled" else "pending", created_at=created, updated_at=created)
            session.add(order)
            await session.flush()
            if state in ("confirmed", "preparing", "delivering"):
                # Isolated synthetic prerequisite, not a provider settlement claim.
                matching = index != 5
                session.add(Payment(order_id=order.order_id, method="cash", amount=total,
                                    currency="CDF", status="completed" if matching else "pending",
                                    completed_at=now if matching else None))
                order.status = "confirmed"
                order.confirmed_at = created + timedelta(hours=1)
                order.updated_at = order.confirmed_at
            await session.commit()
            if state == "confirmed" and index == 6:
                await allocate_paid_order_stock(session, order_id=order.order_id)
                await session.commit()
            if state in ("preparing", "delivering"):
                await allocate_paid_order_stock(session, order_id=order.order_id)
                await session.commit()
                result = await prepare_confirmed_order(session, order_id=order.order_id,
                    expected_updated_at=order.updated_at, idempotency_key=identity("prepare", index),
                    actor_kind="human", actor_display_name=administrator.display_name,
                    actor_account_id=administrator.account_id, actor_role="administrator",
                    request_id=f"ux-review-prepare-{index}")
                if state == "delivering":
                    await dispatch_prepared_order(session, order_id=order.order_id,
                        expected_updated_at=result.order.updated_at, idempotency_key=identity("dispatch", index),
                        handoff_reference=f"FICTIONAL-UX-{index:03d}", attested=True,
                        actor_account_id=administrator.account_id, actor_display_name=administrator.display_name,
                        actor_role="administrator", request_id=f"ux-review-dispatch-{index}")
        count = await session.scalar(select(func.count()).select_from(Order))
        if count != len(NAMES):
            raise RuntimeError(f"Unexpected seeded Order count: {count}")
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
