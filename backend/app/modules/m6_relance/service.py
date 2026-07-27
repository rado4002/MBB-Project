"""
app/modules/m6_relance/service.py — Relance orchestration service.

Main entry points for relance creation, scheduling, and cancellation.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.relance import Relance
from app.modules.m6_relance.hooks import generate_relance_hook
from app.modules.m6_relance.scheduler import calculate_next_relance_time

log = structlog.get_logger(__name__)

MAX_RELANCES = 3  # Hard limit per Phase 1.B spec


async def create_and_schedule_relance(
    session: AsyncSession,
    *,
    lead: Lead,
    conversation: Conversation,
) -> Relance | None:
    """
    Create and schedule next relance for a lead.

    Business rules:
      - Max 3 relances per lead (hard limit)
      - Each relance uses different hook angle
      - Scheduled time adjusted for quiet hours (22:00-07:00 Kinshasa)

    Args:
        session: AsyncSession for DB operations
        lead: Lead instance
        conversation: Conversation instance (for language, last_message_time)

    Returns:
        Relance instance if created, None if max relances reached

    Raises:
        ValueError: If lead.relance_count >= MAX_RELANCES
    """
    # Enforce max relances hard limit
    if lead.relance_count >= MAX_RELANCES:
        log.warning(
            "m6.service.max_relances_reached",
            lead_id=str(lead.lead_id),
            count=lead.relance_count,
        )
        return None

    # Determine next attempt number
    next_attempt = lead.relance_count + 1

    # Get previous hooks to avoid repetition
    previous_hooks = await _get_previous_hook_texts(session, lead.lead_id)

    # Generate value hook using Claude
    hook_text, hook_type = await generate_relance_hook(
        attempt_number=next_attempt,
        language=conversation.language_detected,
        product_interest=lead.product_interest[0] if lead.product_interest else None,
        city=None,
        customer_name=None,
        previous_hooks=previous_hooks,
    )

    # Calculate scheduled send time (with quiet hours adjustment)
    scheduled_at = calculate_next_relance_time(
        last_customer_message_time=conversation.last_message_time,
        attempt_number=next_attempt,
    )

    # Create relance record
    relance = Relance(
        lead_id=lead.lead_id,
        attempt_number=next_attempt,
        scheduled_at=scheduled_at,
        value_hook=hook_text,
        hook_type=hook_type,
    )

    session.add(relance)

    # Update lead relance_count (will be committed by caller)
    lead.relance_count = next_attempt

    log.info(
        "m6.service.relance_scheduled",
        relance_id=str(relance.relance_id),
        lead_id=str(lead.lead_id),
        attempt=next_attempt,
        scheduled_at=str(scheduled_at),
        hook_type=hook_type,
    )

    return relance


async def cancel_all_relances(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
) -> int:
    """
    Cancel all pending (not yet delivered) relances for a lead.

    Used when customer opts out or converts.

    Args:
        session: AsyncSession for DB operations
        lead_id: Lead UUID

    Returns:
        Count of cancelled relances
    """
    from sqlalchemy import and_, select, update

    # Find all pending relances
    query = select(Relance).where(
        and_(
            Relance.lead_id == lead_id,
            Relance.delivered_at.is_(None),
            Relance.cancelled == False,  # noqa: E712
        )
    )

    result = await session.execute(query)
    pending_relances = list(result.scalars().all())

    if not pending_relances:
        return 0

    # Mark all as cancelled
    update_stmt = (
        update(Relance)
        .where(
            and_(
                Relance.lead_id == lead_id,
                Relance.delivered_at.is_(None),
                Relance.cancelled == False,  # noqa: E712
            )
        )
        .values(cancelled=True)
    )

    await session.execute(update_stmt)

    log.info(
        "m6.service.relances_cancelled",
        lead_id=str(lead_id),
        count=len(pending_relances),
    )

    return len(pending_relances)


async def mark_relance_delivered(
    session: AsyncSession,
    *,
    relance_id: uuid.UUID,
) -> None:
    """
    Mark relance as delivered (sent to customer).

    Args:
        session: AsyncSession for DB operations
        relance_id: Relance UUID
    """
    from sqlalchemy import update

    # Update delivered_at
    update_stmt = (
        update(Relance)
        .where(Relance.relance_id == relance_id)
        .values(delivered_at=datetime.now(timezone.utc))
    )

    await session.execute(update_stmt)

    log.info("m6.service.relance_delivered", relance_id=str(relance_id))


async def mark_relance_responded(
    session: AsyncSession,
    *,
    relance_id: uuid.UUID,
    response_time_minutes: int,
) -> None:
    """
    Mark relance as responded (customer replied).

    Args:
        session: AsyncSession for DB operations
        relance_id: Relance UUID
        response_time_minutes: Minutes between delivery and response
    """
    from sqlalchemy import update

    update_stmt = (
        update(Relance)
        .where(Relance.relance_id == relance_id)
        .values(
            response_received=True,
            response_time_minutes=response_time_minutes,
        )
    )

    await session.execute(update_stmt)

    log.info(
        "m6.service.relance_responded",
        relance_id=str(relance_id),
        response_minutes=response_time_minutes,
    )


async def _get_previous_hook_texts(
    session: AsyncSession, lead_id: uuid.UUID
) -> list[str]:
    """Get all previous hook texts for a lead (to avoid repetition)."""
    from sqlalchemy import select

    query = select(Relance.value_hook).where(Relance.lead_id == lead_id).order_by(Relance.attempt_number)

    result = await session.execute(query)
    hooks = [row[0] for row in result.all()]

    return hooks
