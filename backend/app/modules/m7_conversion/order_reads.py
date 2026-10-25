"""Read-only Order summaries for application-layer composition."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.order import Order
from app.models.order_draft import OrderDraft


@dataclass(frozen=True)
class CustomerOrderSummary:
    order_id: UUID
    status: str
    created_at: datetime


async def verified_order_conversations(
    db: AsyncSession,
    order_ids: list[UUID],
) -> dict[UUID, UUID]:
    """Return only Order-to-conversation links verified across owned records."""
    if not order_ids:
        return {}
    rows = await db.execute(
        select(OrderDraft.order_id, OrderDraft.conversation_id)
        .join(Order, Order.order_id == OrderDraft.order_id)
        .join(Conversation, Conversation.conversation_id == OrderDraft.conversation_id)
        .join(Lead, Lead.lead_id == Order.lead_id)
        .where(
            OrderDraft.order_id.in_(order_ids),
            OrderDraft.status == "confirmed",
            Lead.conversation_id == OrderDraft.conversation_id,
            Lead.customer_id == Order.customer_id,
            Conversation.customer_id == Order.customer_id,
        )
    )
    return {order_id: conversation_id for order_id, conversation_id in rows.all()}


async def read_customer_order_summaries(
    db: AsyncSession,
    *,
    customer_id: str,
    limit: int = 5,
) -> list[CustomerOrderSummary]:
    """Read a bounded newest-first Order history for one authoritative customer."""
    orders = list(
        (
            await db.scalars(
                select(Order)
                .where(Order.customer_id == customer_id)
                .order_by(Order.created_at.desc(), Order.order_id.desc())
                .limit(limit)
            )
        ).all()
    )
    return [
        CustomerOrderSummary(
            order_id=order.order_id,
            status=order.status,
            created_at=order.created_at,
        )
        for order in orders
    ]
