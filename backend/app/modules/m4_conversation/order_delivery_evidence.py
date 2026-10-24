"""Read-only customer-message evidence contract for Order delivery."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.message import Message
from app.models.order import Order
from app.models.order_draft import OrderDraft


class CustomerReceiptEvidenceError(Exception):
    """Base failure for customer receipt evidence reads."""


class CustomerReceiptEvidenceMissing(CustomerReceiptEvidenceError):
    pass


class CustomerReceiptEvidenceInvalid(CustomerReceiptEvidenceError):
    pass


class CustomerReceiptEvidenceAmbiguous(CustomerReceiptEvidenceError):
    pass


@dataclass(frozen=True)
class CustomerReceiptEvidence:
    message: Message
    conversation_id: uuid.UUID


async def read_customer_receipt_evidence(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    customer_id: str,
    evidence_message_id: uuid.UUID,
    dispatched_at: datetime,
) -> CustomerReceiptEvidence:
    """Lock and validate one authentic post-dispatch inbound text message.

    Message meaning remains a human decision. This contract establishes only
    provenance, timing and an unambiguous one-delivering-Order association.
    """
    conversation_id = await session.scalar(
        select(OrderDraft.conversation_id)
        .join(Order, Order.order_id == OrderDraft.order_id)
        .join(Conversation, Conversation.conversation_id == OrderDraft.conversation_id)
        .join(Lead, Lead.lead_id == Order.lead_id)
        .where(
            OrderDraft.order_id == order_id,
            OrderDraft.status == "confirmed",
            Order.customer_id == customer_id,
            Lead.conversation_id == OrderDraft.conversation_id,
            Lead.customer_id == customer_id,
            Conversation.customer_id == customer_id,
        )
    )
    if conversation_id is None:
        raise CustomerReceiptEvidenceMissing(
            "Order has no verified customer conversation"
        )

    delivering_count = await session.scalar(
        select(func.count())
        .select_from(OrderDraft)
        .join(Order, Order.order_id == OrderDraft.order_id)
        .join(Conversation, Conversation.conversation_id == OrderDraft.conversation_id)
        .join(Lead, Lead.lead_id == Order.lead_id)
        .where(
            OrderDraft.conversation_id == conversation_id,
            OrderDraft.status == "confirmed",
            Order.status == "delivering",
            Order.customer_id == customer_id,
            Lead.conversation_id == conversation_id,
            Lead.customer_id == customer_id,
            Conversation.customer_id == customer_id,
        )
    )
    if delivering_count != 1:
        raise CustomerReceiptEvidenceAmbiguous(
            "customer message cannot be associated with exactly one delivering Order"
        )

    message = await session.scalar(
        select(Message)
        .where(
            Message.message_id == evidence_message_id,
            Message.conversation_id == conversation_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if message is None:
        raise CustomerReceiptEvidenceMissing("customer receipt message was not found")
    if (
        message.direction != "inbound"
        or message.content_type != "text"
        or message.inbound_receipt_order is None
        or message.inbound_receipt_order <= 0
        or not message.content.strip()
        or not (message.whatsapp_message_id or "").strip()
    ):
        raise CustomerReceiptEvidenceInvalid(
            "customer receipt evidence is not an authentic inbound text message"
        )
    if message.created_at <= dispatched_at:
        raise CustomerReceiptEvidenceInvalid(
            "customer receipt evidence must be persisted after dispatch"
        )
    return CustomerReceiptEvidence(
        message=message,
        conversation_id=conversation_id,
    )
