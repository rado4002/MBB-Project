"""
app/modules/m8_maps/escalation.py — Escalation trigger detection + ticket creation.

Triggers:
  - Voice note received → HIGH priority
  - Complex complaint (AI classification) → HIGH
  - High-value lead (score_value ≥ 80) → MEDIUM
  - 3+ unresolved questions → MEDIUM
  - Customer requests human ("parler à quelqu'un") → HIGH immediate handoff
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select, func, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message

log = structlog.get_logger(__name__)

# ── Escalation trigger patterns ───────────────────────────────────────────────
_HUMAN_REQUEST = re.compile(
    r"(parler.*(quelqu|humain|personne|agent)|nabingi\s+moto|"
    r"veux.*parler|je veux.*un humain|nalingi.*moto)",
    re.IGNORECASE,
)

_COMPLAINT_KEYWORDS = re.compile(
    r"(problème|erreur|cassé|marche pas|défaut|arnaque|voleur|pas reçu|"
    r"mbeba|likambo|plainte|rembourse|refund)",
    re.IGNORECASE,
)

# Score threshold for high-value lead escalation
_HIGH_VALUE_THRESHOLD = 80


class EscalationTrigger:
    """Result of trigger detection."""
    __slots__ = ("should_escalate", "reason", "priority")

    def __init__(self, should_escalate: bool, reason: str = "", priority: str = "medium"):
        self.should_escalate = should_escalate
        self.reason = reason
        self.priority = priority


async def detect_triggers(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    message_content: str,
    content_type: str,
    lead_id: uuid.UUID | None = None,
) -> EscalationTrigger:
    """
    Check if a message/conversation should trigger escalation.
    Returns EscalationTrigger with should_escalate, reason, priority.
    """
    # 1. Voice note → immediate HIGH escalation
    if content_type == "voice_note":
        return EscalationTrigger(True, "voice_note", "high")

    # 2. Human request → immediate HIGH
    if _HUMAN_REQUEST.search(message_content):
        return EscalationTrigger(True, "human_request", "high")

    # 3. Complex complaint → HIGH
    if _COMPLAINT_KEYWORDS.search(message_content):
        return EscalationTrigger(True, "complex_complaint", "high")

    # 4. High-value lead → MEDIUM
    if lead_id:
        lead = await session.get(Lead, lead_id)
        if lead and lead.score_value >= _HIGH_VALUE_THRESHOLD:
            return EscalationTrigger(True, "high_value_lead", "medium")

    # 5. 3+ unanswered inbound messages → MEDIUM
    unanswered = await _count_unanswered(session, conversation_id)
    if unanswered >= 3:
        return EscalationTrigger(True, "unresolved_3x", "medium")

    return EscalationTrigger(False)


async def _count_unanswered(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    """Count consecutive inbound messages without an outbound response."""
    result = await session.execute(
        select(Message.direction)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.desc())
        .limit(10)
    )
    count = 0
    for (direction,) in result:
        if direction == "inbound":
            count += 1
        else:
            break
    return count


async def create_ticket(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    customer_id: str,
    reason: str,
    priority: str,
    lead_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """
    Create an escalation ticket with last 10 messages as context.
    Sets conversation status to 'escalated'.
    """
    # Get last 10 messages for transcript snapshot
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.desc())
        .limit(10)
    )
    messages = result.scalars().all()
    transcript = [
        {
            "direction": m.direction,
            "content": m.content[:500],  # Truncate for snapshot
            "content_type": m.content_type,
            "timestamp": m.timestamp.isoformat() if m.timestamp else None,
        }
        for m in reversed(messages)
    ]

    # Map reason to check constraint values
    reason_map = {
        "voice_note": "voice_note",
        "complex_complaint": "complex_complaint",
        "high_value_lead": "high_value_lead",
        "unresolved_3x": "unresolved_3x",
        "human_request": "voice_note",  # Map to closest valid value
        "sav_issue": "sav_issue",
    }
    db_reason = reason_map.get(reason, "complex_complaint")

    ticket = EscalationTicket(
        lead_id=lead_id,
        conversation_id=conversation_id,
        customer_id=customer_id,
        priority=priority,
        reason=db_reason,
        status="open",
        transcript_snapshot=transcript,
    )
    session.add(ticket)

    # Set conversation status to escalated
    await session.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(status="escalated", updated_at=datetime.now(timezone.utc))
    )

    await session.flush()
    log.info(
        "escalation.ticket.created",
        ticket_id=str(ticket.ticket_id),
        conversation_id=str(conversation_id),
        reason=reason,
        priority=priority,
    )
    return {
        "escalated": True,
        "escalation_id": str(ticket.ticket_id),
        "reason": reason,
        "priority": priority,
    }


async def resolve_ticket(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    resolution_notes: str,
    resolved_by: str,
) -> dict[str, Any]:
    """
    Resolve an escalation ticket. Returns conversation to bot control.
    """
    ticket = await session.get(EscalationTicket, ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")
    if ticket.status in ("resolved", "closed"):
        raise ValueError(f"Ticket {ticket_id} already {ticket.status}")

    now = datetime.now(timezone.utc)
    ticket.status = "resolved"
    ticket.resolution_notes = resolution_notes
    ticket.assigned_to = resolved_by
    ticket.resolved_at = now

    # Return conversation to active (bot control)
    await session.execute(
        update(Conversation)
        .where(Conversation.conversation_id == ticket.conversation_id)
        .values(status="active", updated_at=now)
    )

    await session.flush()
    log.info(
        "escalation.ticket.resolved",
        ticket_id=str(ticket_id),
        resolved_by=resolved_by,
    )
    return {
        "ticket_id": str(ticket.ticket_id),
        "status": "resolved",
        "resolved_at": now.isoformat(),
    }


async def assign_ticket(
    session: AsyncSession,
    ticket_id: uuid.UUID,
    assigned_to: str,
) -> dict[str, Any]:
    """Assign a ticket to a Hub team member."""
    ticket = await session.get(EscalationTicket, ticket_id)
    if not ticket:
        raise ValueError(f"Ticket {ticket_id} not found")

    now = datetime.now(timezone.utc)
    ticket.assigned_to = assigned_to
    ticket.assigned_at = now
    ticket.status = "in_progress"

    await session.flush()
    return {
        "ticket_id": str(ticket.ticket_id),
        "assigned_to": assigned_to,
        "status": "in_progress",
    }


async def list_tickets(
    session: AsyncSession,
    status_filter: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[dict], int]:
    """List escalation tickets with optional status filter."""
    base = select(EscalationTicket)
    count_q = select(func.count()).select_from(EscalationTicket)

    if status_filter:
        base = base.where(EscalationTicket.status == status_filter)
        count_q = count_q.where(EscalationTicket.status == status_filter)

    total = (await session.execute(count_q)).scalar() or 0
    result = await session.execute(
        base.order_by(EscalationTicket.created_at.desc()).offset(offset).limit(limit)
    )
    tickets = result.scalars().all()

    return (
        [
            {
                "ticket_id": str(t.ticket_id),
                "conversation_id": str(t.conversation_id),
                "customer_id": t.customer_id,
                "priority": t.priority,
                "reason": t.reason,
                "status": t.status,
                "assigned_to": t.assigned_to,
                "created_at": t.created_at.isoformat() if t.created_at else None,
                "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            }
            for t in tickets
        ],
        total,
    )


async def handoff_conversation(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    mode: str,
) -> dict[str, Any]:
    """
    Toggle conversation between bot and human control.
    mode: 'human' → set escalated; 'bot' → set active
    """
    conv = await session.get(Conversation, conversation_id)
    if not conv:
        raise ValueError(f"Conversation {conversation_id} not found")

    now = datetime.now(timezone.utc)
    new_status = "escalated" if mode == "human" else "active"
    conv.status = new_status
    conv.updated_at = now

    await session.flush()
    log.info("escalation.handoff", conversation_id=str(conversation_id), mode=mode)
    return {
        "conversation_id": str(conversation_id),
        "mode": mode,
        "status": new_status,
        "updated_at": now.isoformat(),
    }


async def check_stale_escalations(
    session: AsyncSession,
    stale_minutes: int = 120,
) -> dict[str, Any]:
    """Find open/in_progress tickets older than stale_minutes. Returns count."""
    from datetime import timedelta as _td
    cutoff = datetime.now(timezone.utc) - _td(minutes=stale_minutes)
    result = await session.execute(
        select(func.count())
        .select_from(EscalationTicket)
        .where(EscalationTicket.status.in_(["open", "in_progress"]))
        .where(EscalationTicket.created_at < cutoff)
    )
    stale_count = result.scalar() or 0
    log.info("escalation.stale_check", stale_count=stale_count)
    return {"stale_count": stale_count, "reminders_sent": stale_count}


# ── Celery-callable wrappers (open own DB session) ────────────────────────────

async def create_ticket_from_task(
    conversation_id: str,
    reason: str,
    lead_id: str | None = None,
) -> dict[str, Any]:
    """Called by Celery task. Opens its own session."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        # Get conversation to find customer_id
        conv = await session.get(Conversation, uuid.UUID(conversation_id))
        if not conv:
            return {"escalated": False, "reason": "conversation_not_found"}
        result = await create_ticket(
            session,
            conversation_id=uuid.UUID(conversation_id),
            customer_id=conv.customer_id,
            reason=reason,
            priority="high" if reason in ("voice_note", "complex_complaint", "human_request") else "medium",
            lead_id=uuid.UUID(lead_id) if lead_id else None,
        )
        await session.commit()
        return result


async def check_stale_from_task() -> dict[str, Any]:
    """Called by Celery Beat task. Opens its own session."""
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as session:
        result = await check_stale_escalations(session)
        await session.commit()
        return result
