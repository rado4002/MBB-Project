"""
app/tasks/relance.py — Celery tasks for M5 Relance Engine.

Relance rules (DRC constraints):
  - Max 3 relances per lead
  - Schedule: +24h → +48–72h → +7–10d
  - No sends between 22:00–07:00 Kinshasa time (UTC+2)
  - Each attempt uses a different angle / hook
  - Idempotent: re-queueing the same relance_id is safe
"""

from __future__ import annotations

import structlog
from celery import Task

from app.tasks.celery_app import celery_app

log = structlog.get_logger(__name__)


# ── Base task with retry defaults ─────────────────────────────────────────────

class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 60  # seconds; individual tasks override as needed


# ── Task: send a single relance message ───────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.relance.send_relance",
    queue="relance",
    acks_late=True,
)
def send_relance(self: Task, relance_id: str, lead_id: str, attempt: int) -> dict:
    """
    Send one relance message for *lead_id*.

    Args:
        relance_id: UUID of the Relance row (idempotency key).
        lead_id:    UUID of the Lead row.
        attempt:    1 | 2 | 3 (controls the persuasion hook angle).

    Returns dict with {"status": "sent" | "skipped", "relance_id": ...}.
    """
    import asyncio

    from app.modules.m6_relance import service as relance_svc  # type: ignore[import]

    log.info("relance.task.start", relance_id=relance_id, lead_id=lead_id, attempt=attempt)
    try:
        result = asyncio.run(
            relance_svc.execute_relance(relance_id=relance_id, lead_id=lead_id, attempt=attempt)
        )
        log.info("relance.task.done", relance_id=relance_id, status=result.get("status"))
        return result
    except Exception as exc:
        log.error("relance.task.error", relance_id=relance_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


# ── Task: schedule next relance for a lead ────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.relance.schedule_next",
    queue="relance",
    acks_late=True,
)
def schedule_next_relance(self: Task, lead_id: str, current_attempt: int) -> dict:
    """
    Determine if a follow-up relance should be scheduled and enqueue it
    with the correct countdown (in seconds).

    Countdown schedule:
      attempt 1 → attempt 2:  +48h  (172_800 s)
      attempt 2 → attempt 3:  +7d   (604_800 s)
      attempt 3 → no more
    """
    import asyncio

    from app.modules.m6_relance import service as relance_svc  # type: ignore[import]

    log.info("relance.schedule_next.start", lead_id=lead_id, current_attempt=current_attempt)
    try:
        result = asyncio.run(
            relance_svc.schedule_next(lead_id=lead_id, current_attempt=current_attempt)
        )
        return result
    except Exception as exc:
        log.error("relance.schedule_next.error", lead_id=lead_id, error=str(exc))
        raise self.retry(exc=exc, countdown=120)


# ── Periodic task: scan for due relances (Beat-triggered) ─────────────────────

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
    import asyncio

    from app.modules.m6_relance import service as relance_svc  # type: ignore[import]

    log.info("relance.process_due.start")
    result = asyncio.run(
        relance_svc.process_due_relances()
    )
    log.info("relance.process_due.done", dispatched=result.get("dispatched", 0))
    return result
