"""Relance V2 candidate selection. No delivery is performed here."""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message
from app.models.relance_candidate import RelanceCandidate
from app.modules.m6_relance.scheduler import get_next_allowed_time


async def create_candidates(
    session: AsyncSession, *, delay_hours: float, now: datetime | None = None
) -> tuple[int, int]:
    """Scan Lead-backed conversations and persist eligible candidates.

    Customer then conversation locks match inbound processing's lock order.
    Uniqueness indexes protect against concurrent scans and repeat episodes.
    """
    now = now or datetime.now(timezone.utc)
    lead_ids = (await session.scalars(select(Lead.lead_id).order_by(Lead.lead_id))).all()
    eligible_count = 0
    created_count = 0

    for lead_id in lead_ids:
        lead = await session.get(Lead, lead_id)
        if lead is None:
            continue
        customer = await session.scalar(
            select(Customer).where(Customer.phone_number == lead.customer_id).with_for_update()
        )
        if customer is None or customer.opt_out_flag:
            continue
        conversation = await session.scalar(
            select(Conversation)
            .where(Conversation.conversation_id == lead.conversation_id)
            .with_for_update()
        )
        if (
            conversation is None
            or conversation.customer_id != customer.phone_number
            or conversation.owner_type != "ai"
            or conversation.ai_execution_state != "eligible"
        ):
            continue

        active_escalation = await session.scalar(
            select(EscalationTicket.ticket_id)
            .where(
                EscalationTicket.conversation_id == conversation.conversation_id,
                EscalationTicket.status.in_(("open", "in_progress")),
            )
            .limit(1)
        )
        if active_escalation is not None:
            continue

        last_inbound = await session.scalar(
            select(Message)
            .where(
                Message.conversation_id == conversation.conversation_id,
                Message.direction == "inbound",
            )
            .order_by(Message.created_at.desc(), Message.timestamp.desc(), Message.message_id.desc())
            .limit(1)
        )
        if last_inbound is None:
            continue
        # A late webhook must not make a newly persisted inbound look old.
        inbound_at = max(last_inbound.timestamp, last_inbound.created_at)
        due_at = inbound_at + timedelta(hours=delay_hours)
        if due_at > now:
            continue

        sent_count = await session.scalar(
            select(func.count())
            .select_from(RelanceCandidate)
            .where(
                RelanceCandidate.lead_id == lead_id,
                RelanceCandidate.confirmed_sent_at.is_not(None),
            )
        )
        if (sent_count or 0) >= 2:
            continue
        attempt_number = (sent_count or 0) + 1

        active = await session.scalar(
            select(RelanceCandidate.candidate_id)
            .where(
                RelanceCandidate.lead_id == lead_id,
                RelanceCandidate.cancelled_at.is_(None),
                RelanceCandidate.confirmed_sent_at.is_(None),
            )
            .limit(1)
        )
        if active is not None:
            continue
        eligible_count += 1

        # An inbound ends its silence episode; do not recreate a cancelled
        # attempt until another inbound begins a new episode.
        prior = await session.scalar(
            select(RelanceCandidate.candidate_id)
            .where(
                RelanceCandidate.lead_id == lead_id,
                RelanceCandidate.source_message_id == last_inbound.message_id,
                RelanceCandidate.attempt_number == attempt_number,
            )
            .limit(1)
        )
        if prior is not None:
            continue

        session.add(
            RelanceCandidate(
                lead_id=lead_id,
                source_message_id=last_inbound.message_id,
                attempt_number=attempt_number,
                scheduled_at=get_next_allowed_time(now),
            )
        )
        await session.flush()
        created_count += 1

    return eligible_count, created_count


async def cancel_active_candidates(
    session: AsyncSession, *, conversation_id: uuid.UUID,
    opted_out_customer_id: str | None = None,
    now: datetime | None = None,
) -> int:
    """Cancel this conversation's candidate, or all customer candidates on opt-out."""
    now = now or datetime.now(timezone.utc)
    lead_ids = select(Lead.lead_id).where(
        Lead.customer_id == opted_out_customer_id
        if opted_out_customer_id is not None
        else Lead.conversation_id == conversation_id
    )
    result = await session.execute(
        update(RelanceCandidate)
        .where(
            RelanceCandidate.lead_id.in_(lead_ids),
            RelanceCandidate.cancelled_at.is_(None),
            RelanceCandidate.confirmed_sent_at.is_(None),
        )
        .values(cancelled_at=now)
    )
    return result.rowcount
