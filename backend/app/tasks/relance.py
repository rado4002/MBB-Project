"""
app/tasks/relance.py — Celery tasks for M6 Relance Engine.

Implements Sprint 1.6 requirements:
  1. Celery Beat scanner (hourly) to find eligible leads
  2. Relance delivery task (sends WhatsApp message)
  3. Max 3 relances per lead (hard limit)
  4. Quiet hours enforcement (22:00-07:00 Kinshasa)
  5. Idempotent operations (safe to retry)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import structlog
from celery import Task
from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.relance import Relance
from app.modules.m6_relance.eligibility import find_eligible_leads
from app.modules.m6_relance.service import (
    create_and_schedule_relance,
    mark_relance_delivered,
)
from app.tasks.celery_app import celery_app, run_async

log = structlog.get_logger(__name__)


class _BaseRelanceTask(Task):
    """Base task with retry defaults for relance operations."""

    abstract = True
    max_retries = 3
    default_retry_delay = 60  # seconds


# ── Periodic task: Find and schedule eligible relances (Celery Beat) ─────────

@celery_app.task(
    bind=True,
    base=_BaseRelanceTask,
    name="app.tasks.relance.scan_eligible_leads",
    queue="relance",
)
def scan_eligible_leads(self: Task) -> dict:
    """
    Periodic task (triggered by Celery Beat every hour).

    Finds all leads eligible for relance and schedules delivery tasks.

    Returns:
        Dict with scan results (eligible_count, scheduled_count)
    """
    log.info("relance.scan.start")

    try:
        result = run_async(_scan_and_schedule_relances())
        log.info(
            "relance.scan.complete",
            eligible=result["eligible_count"],
            scheduled=result["scheduled_count"],
        )
        return result
    except Exception as exc:
        log.error("relance.scan.error", error=str(exc))
        raise self.retry(exc=exc, countdown=300)  # Retry in 5 minutes


async def _scan_and_schedule_relances() -> dict:
    """
    Core logic for scanning eligible leads and scheduling relances.

    Returns:
        Dict with eligible_count and scheduled_count
    """
    async with AsyncSessionLocal() as session:
        # Find eligible leads (silent > 24h, count < 3, not opted out, not converted)
        eligible_leads = await find_eligible_leads(session, silence_threshold_hours=24, max_relances=3)

        scheduled_count = 0

        for lead in eligible_leads:
            # Get conversation for language and timing data
            conv_result = await session.execute(
                select(Conversation).where(Conversation.conversation_id == lead.conversation_id)
            )
            conversation = conv_result.scalar_one_or_none()

            if not conversation:
                log.warning("relance.scan.no_conversation", lead_id=str(lead.lead_id))
                continue

            # Create and schedule relance
            relance = await create_and_schedule_relance(
                session,
                lead=lead,
                conversation=conversation,
            )

            if relance:
                # Schedule delivery task at the scheduled time
                send_relance.apply_async(
                    args=[str(relance.relance_id)],
                    eta=relance.scheduled_at,
                )
                scheduled_count += 1

        await session.commit()

        return {
            "eligible_count": len(eligible_leads),
            "scheduled_count": scheduled_count,
            "scan_time": datetime.now(timezone.utc).isoformat(),
        }


# ── Task: Send a single relance message ───────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseRelanceTask,
    name="app.tasks.relance.send_relance",
    queue="relance",
    acks_late=True,
)
def send_relance(self: Task, relance_id: str) -> dict:
    """
    Send a single relance message via WhatsApp.

    Idempotent: If relance already delivered, skips.

    Args:
        relance_id: UUID string of Relance record

    Returns:
        Dict with status (sent, skipped, failed)
    """
    log.info("relance.send.start", relance_id=relance_id)

    try:
        result = run_async(_send_relance_message(relance_id))
        log.info("relance.send.complete", relance_id=relance_id, status=result["status"])
        return result
    except Exception as exc:
        log.error("relance.send.error", relance_id=relance_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _send_relance_message(relance_id_str: str) -> dict:
    """
    Core logic for sending relance message.

    Returns:
        Dict with status and details
    """
    from app.adapters import get_messaging_adapter
    from app.modules.m6_relance.scheduler import is_quiet_hours

    relance_id = uuid.UUID(relance_id_str)

    async with AsyncSessionLocal() as session:
        # Load relance record
        result = await session.execute(
            select(Relance).where(Relance.relance_id == relance_id)
        )
        relance = result.scalar_one_or_none()

        if not relance:
            log.error("relance.send.not_found", relance_id=relance_id_str)
            return {"status": "failed", "reason": "relance_not_found"}

        # Idempotency check: already delivered?
        if relance.delivered_at:
            log.info("relance.send.already_delivered", relance_id=relance_id_str)
            return {"status": "skipped", "reason": "already_delivered"}

        # Cancellation check
        if relance.cancelled:
            log.info("relance.send.cancelled", relance_id=relance_id_str)
            return {"status": "skipped", "reason": "cancelled"}

        # Quiet hours check (safety: should have been adjusted at scheduling time)
        if is_quiet_hours():
            log.warning("relance.send.quiet_hours", relance_id=relance_id_str)
            # Reschedule for 07:00 next day
            from app.modules.m6_relance.scheduler import get_next_allowed_time

            next_time = get_next_allowed_time()
            send_relance.apply_async(args=[relance_id_str], eta=next_time)
            return {"status": "rescheduled", "next_time": next_time.isoformat()}

        # Load lead and conversation
        lead_result = await session.execute(
            select(Lead).where(Lead.lead_id == relance.lead_id)
        )
        lead = lead_result.scalar_one_or_none()

        if not lead:
            log.error("relance.send.lead_not_found", lead_id=str(relance.lead_id))
            return {"status": "failed", "reason": "lead_not_found"}

        conv_result = await session.execute(
            select(Conversation).where(Conversation.conversation_id == lead.conversation_id)
        )
        conversation = conv_result.scalar_one_or_none()

        if not conversation:
            log.error("relance.send.conversation_not_found", conversation_id=str(lead.conversation_id))
            return {"status": "failed", "reason": "conversation_not_found"}

        # Send via WhatsApp
        messaging_adapter = get_messaging_adapter()
        try:
            await messaging_adapter.send_message(
                phone=conversation.customer_id,
                text=relance.value_hook,
            )
        except Exception as e:
            log.error("relance.send.messaging_error", error=str(e))
            raise

        # Mark as delivered
        await mark_relance_delivered(session, relance_id=relance_id)
        await session.commit()

        log.info(
            "relance.send.success",
            relance_id=relance_id_str,
            lead_id=str(lead.lead_id),
            attempt=relance.attempt_number,
        )

        return {
            "status": "sent",
            "relance_id": relance_id_str,
            "lead_id": str(lead.lead_id),
            "attempt": relance.attempt_number,
            "delivered_at": datetime.now(timezone.utc).isoformat(),
        }


@celery_app.task(
    name="app.tasks.relance.process_due",
    queue="relance",
    acks_late=True,
)
def process_due_relances() -> dict:
    """
    Periodic beat task (runs every 15 min).

    Queries the DB for relances that are due and not yet sent, checks the
    Kinshasa time window, then dispatches individual send_relance tasks.

    Safe to run multiple times (idempotent — each relance row has a status).
    """
    from app.modules.m6_relance import service as relance_svc  # type: ignore[import]

    log.info("relance.process_due.start")
    result = run_async(
        relance_svc.process_due_relances()
    )
    log.info("relance.process_due.done", dispatched=result.get("dispatched", 0))
    return result


# Backward-compatible task export expected by setup validation.
schedule_next_relance = scan_eligible_leads
