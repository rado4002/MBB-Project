"""Offline-only Relance delivery contract; no task or production adapter wiring."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.relance_candidate import RelanceCandidate
from app.models.relance_delivery import RelanceDelivery
from app.modules.m6_relance.readiness import check_delivery_readiness
from app.modules.m6_relance.scheduler import get_next_allowed_time


class TemplateTransport(Protocol):
    offline_only: bool

    async def send_template(
        self, phone: str, template_name: str, params: list[str], *,
        locale: str, idempotency_key: str,
    ) -> str: ...


class DefiniteDeliveryFailure(Exception):
    """Transport explicitly confirms no provider acceptance occurred."""


async def _locked_context(session: AsyncSession, candidate_id: uuid.UUID):
    # Match inbound's customer-first row lock order after read-only key lookup.
    candidate = await session.get(RelanceCandidate, candidate_id)
    if candidate is None:
        return None
    lead = await session.get(Lead, candidate.lead_id)
    if lead is None:
        return None
    customer = await session.scalar(
        select(Customer).where(Customer.phone_number == lead.customer_id).with_for_update()
    )
    if customer is None:
        return None
    conversation = await session.scalar(
        select(Conversation)
        .where(Conversation.conversation_id == lead.conversation_id)
        .with_for_update()
    )
    candidate = await session.scalar(
        select(RelanceCandidate)
        .where(RelanceCandidate.candidate_id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if conversation is None or candidate is None:
        return None
    return candidate, lead, customer, conversation


async def _current_reason(
    session: AsyncSession, *, candidate: RelanceCandidate, lead: Lead,
    conversation: Conversation, settings: Settings, now: datetime,
):
    if candidate.cancelled_at is not None or candidate.confirmed_sent_at is not None:
        return "inactive_candidate", None
    readiness = await check_delivery_readiness(
        session, candidate_id=candidate.candidate_id, settings=settings
    )
    if readiness.status != "ready_for_delivery":
        return readiness.reason or "not_ready", None
    latest = await session.scalar(
        select(Message)
        .where(Message.conversation_id == conversation.conversation_id,
               Message.direction == "inbound")
        .order_by(Message.created_at.desc(), Message.timestamp.desc(), Message.message_id.desc())
        .limit(1)
    )
    if latest is None or latest.message_id != candidate.source_message_id:
        return "new_inbound", None
    inbound_at = max(latest.timestamp, latest.created_at)
    if inbound_at + timedelta(hours=settings.relance_delay_1_hours) > now:
        return "silence_not_due", None
    sent_count = await session.scalar(
        select(func.count()).select_from(RelanceCandidate).where(
            RelanceCandidate.lead_id == lead.lead_id,
            RelanceCandidate.confirmed_sent_at.is_not(None),
        )
    )
    if (sent_count or 0) >= 2 or candidate.attempt_number != (sent_count or 0) + 1:
        return "attempt_limit", None
    if candidate.scheduled_at > now or get_next_allowed_time(now) > now:
        return "outside_allowed_hours", None
    return None, readiness


async def _cancel(
    session: AsyncSession, candidate: RelanceCandidate, delivery: RelanceDelivery | None,
    reason: str, now: datetime,
) -> RelanceDelivery:
    if candidate.confirmed_sent_at is None and candidate.cancelled_at is None:
        candidate.cancelled_at = now
    if delivery is None:
        delivery = RelanceDelivery(candidate_id=candidate.candidate_id, status="cancelled")
        session.add(delivery)
    elif delivery.outbound_message_id is not None and delivery.dispatch_started_at is None:
        # The reserved row was never submitted; do not show a phantom outbound.
        message = await session.get(Message, delivery.outbound_message_id)
        delivery.outbound_message_id = None
        await session.flush()
        if message is not None:
            await session.delete(message)
    delivery.status = "cancelled"
    delivery.reason = reason
    delivery.updated_at = now
    return delivery


async def prepare_candidate_delivery(
    factory: async_sessionmaker[AsyncSession], *, candidate_id: uuid.UUID,
    settings: Settings, now: datetime | None = None,
) -> tuple[str, uuid.UUID | None]:
    """Reserve one outbound UUID; a stale preparation is never auto-dispatched."""
    now = now or datetime.now(timezone.utc)
    async with factory() as session:
        context = await _locked_context(session, candidate_id)
        if context is None:
            return "cancelled", None
        candidate, lead, _, conversation = context
        delivery = await session.get(RelanceDelivery, candidate_id)
        if delivery is not None:
            return (
                "already_prepared" if delivery.status == "prepared" else delivery.status,
                delivery.outbound_message_id,
            )
        if candidate.confirmed_sent_at is not None:
            return "blocked", None
        reason, readiness = await _current_reason(
            session, candidate=candidate, lead=lead, conversation=conversation,
            settings=settings, now=now,
        )
        if reason is not None:
            # Future scheduled candidates remain pending; changed authority closes them.
            if reason == "outside_allowed_hours" or reason == "silence_not_due":
                return "blocked", None
            await _cancel(session, candidate, None, reason, now)
            await session.commit()
            return "cancelled", None
        message_id = uuid.uuid4()
        session.add(Message(
            message_id=message_id, conversation_id=conversation.conversation_id,
            timestamp=now, direction="outbound", content=(
                f"[WhatsApp template: {readiness.template_name} ({readiness.template_locale})]"
            ), content_type="text", language=readiness.language,
            delivery_state=None, delivery_state_timestamp=None,
        ))
        session.add(RelanceDelivery(
            candidate_id=candidate_id, outbound_message_id=message_id,
            status="prepared", reason="reserved_not_dispatched", updated_at=now,
        ))
        await session.commit()
        return "prepared", message_id


async def dispatch_prepared_delivery(
    factory: async_sessionmaker[AsyncSession], *, candidate_id: uuid.UUID,
    outbound_message_id: uuid.UUID, transport: TemplateTransport,
    settings: Settings, now: datetime | None = None,
) -> str:
    """Claim durably, then recheck under inbound's lock and call once."""
    if getattr(transport, "offline_only", False) is not True:
        raise ValueError("Relance V2-E accepts offline transport only")
    now = now or datetime.now(timezone.utc)
    async with factory() as session:
        context = await _locked_context(session, candidate_id)
        if context is None:
            return "cancelled"
        candidate, lead, customer, conversation = context
        delivery = await session.scalar(
            select(RelanceDelivery)
            .where(RelanceDelivery.candidate_id == candidate_id)
            .with_for_update()
        )
        if delivery is None or delivery.outbound_message_id != outbound_message_id:
            return "cancelled"
        if delivery.status != "prepared" or delivery.reason != "reserved_not_dispatched":
            return delivery.status
        reason, readiness = await _current_reason(
            session, candidate=candidate, lead=lead, conversation=conversation,
            settings=settings, now=now,
        )
        if reason is not None:
            await _cancel(session, candidate, delivery, reason, now)
            await session.commit()
            return "cancelled"
        message = await session.get(Message, outbound_message_id)
        if message is None:
            # Lost outbound evidence cannot authorize a send.
            await _cancel(session, candidate, delivery, "missing_outbound", now)
            await session.commit()
            return "cancelled"
        delivery.dispatch_started_at = now
        delivery.status = "uncertain"
        delivery.reason = "provider_outcome_unknown"
        message.delivery_state = "uncertain"
        message.delivery_state_timestamp = now
        await session.commit()

    # Persist the one-way dispatch claim before touching the provider. A crash
    # after acceptance now leaves an uncertain row that cannot be dispatched
    # again by a duplicate or manual invocation of this entry point.
    async with factory() as session:
        context = await _locked_context(session, candidate_id)
        if context is None:
            return "uncertain"
        candidate, lead, customer, conversation = context
        delivery = await session.scalar(
            select(RelanceDelivery)
            .where(RelanceDelivery.candidate_id == candidate_id)
            .with_for_update()
        )
        if delivery is None or delivery.outbound_message_id != outbound_message_id:
            return "uncertain"
        reason, readiness = await _current_reason(
            session, candidate=candidate, lead=lead, conversation=conversation,
            settings=settings, now=now,
        )
        if reason is not None:
            delivery.dispatch_started_at = None  # no provider call occurred
            await _cancel(session, candidate, delivery, reason, now)
            await session.commit()
            return "cancelled"
        message = await session.get(Message, outbound_message_id)
        if message is None:
            delivery.dispatch_started_at = None
            await _cancel(session, candidate, delivery, "missing_outbound", now)
            await session.commit()
            return "cancelled"
        try:
            provider_id = await transport.send_template(
                customer.phone_number, readiness.template_name, [],
                locale=readiness.template_locale,
                idempotency_key=str(message.message_id),
            )
        except DefiniteDeliveryFailure:
            delivery.status = "failed"
            delivery.reason = "definite_failure"
            message.delivery_state = "failed"
        except Exception:
            delivery.status = "uncertain"
            delivery.reason = "provider_outcome_unknown"
            message.delivery_state = "uncertain"
        else:
            if isinstance(provider_id, str) and 0 < len(provider_id.strip()) <= 100:
                delivery.status = "sent"
                delivery.reason = None
                delivery.provider_message_id = provider_id.strip()
                candidate.confirmed_sent_at = now
                message.whatsapp_message_id = provider_id.strip()
                message.delivery_state = "sent"
            else:
                delivery.status = "uncertain"
                delivery.reason = "invalid_provider_response"
                message.delivery_state = "uncertain"
        delivery.updated_at = now
        message.delivery_state_timestamp = now
        await session.commit()
        return delivery.status


async def deliver_candidate(
    factory: async_sessionmaker[AsyncSession], *, candidate_id: uuid.UUID,
    transport: TemplateTransport, settings: Settings,
    now: datetime | None = None,
) -> tuple[str, uuid.UUID | None]:
    """Offline entry point; caller supplies a fake transport, never a task adapter."""
    if getattr(transport, "offline_only", False) is not True:
        raise ValueError("Relance V2-E accepts offline transport only")
    status, message_id = await prepare_candidate_delivery(
        factory, candidate_id=candidate_id, settings=settings, now=now,
    )
    if status == "already_prepared":
        return "prepared", message_id
    if status != "prepared" or message_id is None:
        return status, message_id
    result = await dispatch_prepared_delivery(
        factory, candidate_id=candidate_id, outbound_message_id=message_id,
        transport=transport, settings=settings, now=now,
    )
    return result, message_id if result != "cancelled" else None
