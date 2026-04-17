"""
app/tasks/escalation.py — Celery tasks for M8 Escalation System.

Triggers when M2/M3 detect:
  - Voice note message (media type = audio)
  - Complex objection or complaint
  - High-value lead (score = hot AND order value > threshold)
  - Explicit human-agent request ("parler à quelqu'un", "nabingi moto")

Escalated conversations are flagged in the DB and a notification is
pushed to the Hub Team via the CRM adapter.
"""

from __future__ import annotations

import structlog
from celery import Task

from app.tasks.celery_app import celery_app

log = structlog.get_logger(__name__)


class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 30


# ── Task: escalate a conversation ─────────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.escalation.escalate_conversation",
    queue="escalation",
    acks_late=True,
)
def escalate_conversation(
    self: Task,
    conversation_id: str,
    reason: str,
    lead_id: str | None = None,
) -> dict:
    """
    Mark *conversation_id* as escalated and notify the Hub Team.

    Args:
        conversation_id: UUID of the Conversation row.
        reason:          e.g. "voice_note", "complex_issue", "high_value_lead".
        lead_id:         Optional UUID of the related Lead row.

    Returns dict with {"escalated": bool, "escalation_id": str}.
    """
    import asyncio

    from app.modules.m8_maps import escalation as escalation_svc  # type: ignore[import]

    log.info(
        "escalation.task.start",
        conversation_id=conversation_id,
        reason=reason,
        lead_id=lead_id,
    )
    try:
        result = asyncio.run(
            escalation_svc.create_ticket_from_task(
                conversation_id=conversation_id,
                reason=reason,
                lead_id=lead_id,
            )
        )
        log.info("escalation.task.done", escalation_id=result.get("escalation_id"))
        return result
    except Exception as exc:
        log.error("escalation.task.error", conversation_id=conversation_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)


# ── Task: check for stale escalations (Beat-triggered) ───────────────────────

@celery_app.task(
    name="app.tasks.escalation.check_stale",
    queue="escalation",
    acks_late=True,
)
def check_stale_escalations() -> dict:
    """
    Periodic beat task (runs every 30 min).

    Finds escalations that have been pending > 2 hours and sends a reminder
    to the Hub Team. Prevents leads from being forgotten during busy periods.
    """
    import asyncio

    from app.modules.m8_maps import escalation as escalation_svc  # type: ignore[import]

    log.info("escalation.check_stale.start")
    result = asyncio.run(
        escalation_svc.check_stale_from_task()
    )
    log.info("escalation.check_stale.done", reminders_sent=result.get("reminders_sent", 0))
    return result
