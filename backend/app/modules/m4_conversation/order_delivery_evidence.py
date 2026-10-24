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
from app.models.order_delivery_confirmation import OrderDeliveryConfirmation
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


@dataclass(frozen=True)
class CustomerReceiptEvidencePreview:
    message_id: uuid.UUID
    content_excerpt: str
    content_truncated: bool
    received_at: datetime
    inbound_receipt_order: int


@dataclass(frozen=True)
class CustomerReceiptEvidenceCandidates:
    association_state: str
    evidence_state: str
    conversation_id: uuid.UUID | None
    items: tuple[CustomerReceiptEvidencePreview, ...]
    has_more: bool


async def _verified_conversation_id(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    customer_id: str,
) -> uuid.UUID | None:
    return await session.scalar(
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


async def _delivering_order_count(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    customer_id: str,
) -> int:
    count = await session.scalar(
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
    return int(count or 0)


async def list_customer_receipt_evidence_candidates(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    customer_id: str,
    dispatched_at: datetime,
    limit: int = 20,
    excerpt_limit: int = 500,
) -> CustomerReceiptEvidenceCandidates:
    """Return bounded provenance-checked previews; never infer message meaning."""
    if not 1 <= limit <= 20 or not 1 <= excerpt_limit <= 500:
        raise ValueError("delivery evidence preview bounds are invalid")
    conversation_id = await _verified_conversation_id(
        session,
        order_id=order_id,
        customer_id=customer_id,
    )
    if conversation_id is None:
        return CustomerReceiptEvidenceCandidates(
            association_state="missing",
            evidence_state="not_applicable",
            conversation_id=None,
            items=(),
            has_more=False,
        )
    if await _delivering_order_count(
        session,
        conversation_id=conversation_id,
        customer_id=customer_id,
    ) != 1:
        return CustomerReceiptEvidenceCandidates(
            association_state="ambiguous",
            evidence_state="not_applicable",
            conversation_id=conversation_id,
            items=(),
            has_more=False,
        )
    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.direction == "inbound",
                    Message.content_type == "text",
                    Message.inbound_receipt_order.is_not(None),
                    Message.inbound_receipt_order > 0,
                    func.btrim(Message.content) != "",
                    Message.whatsapp_message_id.is_not(None),
                    func.btrim(Message.whatsapp_message_id) != "",
                    Message.created_at > dispatched_at,
                    ~select(OrderDeliveryConfirmation.confirmation_id)
                    .where(
                        OrderDeliveryConfirmation.evidence_message_id
                        == Message.message_id
                    )
                    .exists(),
                )
                .order_by(
                    Message.created_at.desc(),
                    Message.inbound_receipt_order.desc(),
                    Message.message_id.desc(),
                )
                .limit(limit + 1)
            )
        ).all()
    )
    shown = messages[:limit]
    return CustomerReceiptEvidenceCandidates(
        association_state="verified",
        evidence_state="available" if shown else "missing",
        conversation_id=conversation_id,
        items=tuple(
            CustomerReceiptEvidencePreview(
                message_id=message.message_id,
                content_excerpt=message.content[:excerpt_limit],
                content_truncated=len(message.content) > excerpt_limit,
                received_at=message.created_at,
                inbound_receipt_order=message.inbound_receipt_order,
            )
            for message in shown
        ),
        has_more=len(messages) > limit,
    )


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
    conversation_id = await _verified_conversation_id(
        session,
        order_id=order_id,
        customer_id=customer_id,
    )
    if conversation_id is None:
        raise CustomerReceiptEvidenceMissing(
            "Order has no verified customer conversation"
        )

    delivering_count = await _delivering_order_count(
        session,
        conversation_id=conversation_id,
        customer_id=customer_id,
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
