"""
app/modules/m6_relance/eligibility.py — Eligible lead detection for relances.

Finds leads that are:
  - Silent > 24h (from last customer message)
  - Relance count < 3
  - Not opted out
  - Not converted
  - Not already scheduled for a relance
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.relance import Relance

log = structlog.get_logger(__name__)


async def find_eligible_leads(
    session: AsyncSession,
    *,
    silence_threshold_hours: int = 24,
    max_relances: int = 3,
) -> list[Lead]:
    """
    Find leads eligible for relance.

    Eligibility criteria:
      1. Last customer message > silence_threshold_hours ago
      2. relance_count < max_relances (hard limit: 3)
      3. Customer not opted out (customer.opted_out = False)
      4. Lead not converted (lead.converted_at IS NULL)
      5. No pending relance already scheduled

    Args:
        session: AsyncSession for DB queries
        silence_threshold_hours: Hours of silence before relance eligible (default: 24)
        max_relances: Maximum relances per lead (default: 3, NEVER exceed)

    Returns:
        List of Lead instances eligible for relance
    """
    now = datetime.now(timezone.utc)
    silence_cutoff = now - timedelta(hours=silence_threshold_hours)

    # Query: Leads with all eligibility conditions
    query = (
        select(Lead)
        .join(Conversation, Lead.conversation_id == Conversation.conversation_id)
        .join(Customer, Lead.customer_id == Customer.phone_number)
        .where(
            and_(
                # Silence threshold
                Conversation.last_message_time < silence_cutoff,
                # Max relances not reached
                Lead.relance_count < max_relances,
                # Not opted out
                Customer.opted_out == False,  # noqa: E712
                # Not converted
                Lead.converted_at.is_(None),
                # Not in "dormant" conversation state
                Conversation.status != "dormant",
            )
        )
    )

    result = await session.execute(query)
    eligible_leads = list(result.scalars().all())

    # Additional filter: No pending relance already scheduled
    final_eligible = []
    for lead in eligible_leads:
        has_pending = await _has_pending_relance(session, lead.lead_id)
        if not has_pending:
            final_eligible.append(lead)

    log.info(
        "m6.eligibility.found",
        eligible_count=len(final_eligible),
        total_checked=len(eligible_leads),
        silence_threshold_hours=silence_threshold_hours,
    )

    return final_eligible


async def _has_pending_relance(session: AsyncSession, lead_id: uuid.UUID) -> bool:
    """Check if lead has a pending (undelivered, not cancelled) relance."""
    import uuid

    query = select(Relance).where(
        and_(
            Relance.lead_id == lead_id,
            Relance.delivered_at.is_(None),
            Relance.cancelled == False,  # noqa: E712
        )
    )
    result = await session.execute(query)
    pending = result.scalar_one_or_none()
    return pending is not None


async def get_last_relance_for_lead(
    session: AsyncSession, lead_id: uuid.UUID
) -> Relance | None:
    """Get the most recent relance for a lead (by attempt_number)."""
    import uuid

    query = (
        select(Relance)
        .where(Relance.lead_id == lead_id)
        .order_by(Relance.attempt_number.desc())
        .limit(1)
    )
    result = await session.execute(query)
    return result.scalar_one_or_none()
