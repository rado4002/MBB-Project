"""Hourly no-send Relance V2 candidate scan and inert legacy task names."""
from __future__ import annotations

from datetime import datetime, timezone

import structlog
from celery import Task
from app.config import get_settings
from app.database import AsyncSessionLocal
from app.modules.m6_relance.candidates import create_candidates
from app.tasks.celery_app import celery_app, run_async

log = structlog.get_logger(__name__)
settings = get_settings()


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

    Finds eligible leads and persists candidates without dispatching sends.

    Returns:
        Dict with scan results (eligible_count, scheduled_count)
    """
    log.info("relance.scan.start")

    if not settings.relance_enabled:
        log.warning("relance.scan.skipped_safety_gate", relance_enabled=False)
        return {
            "status": "skipped",
            "reason": "relance_disabled",
            "eligible_count": 0,
            "scheduled_count": 0,
        }

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
    Core logic for scanning eligible leads and persisting candidates.

    Returns:
        Dict with eligible_count and scheduled_count
    """
    async with AsyncSessionLocal() as session:
        eligible_count, created_count = await create_candidates(
            session, delay_hours=settings.relance_delay_1_hours
        )
        await session.commit()

        return {
            "eligible_count": eligible_count,
            "scheduled_count": created_count,
            "candidate_count": created_count,
            "scan_time": datetime.now(timezone.utc).isoformat(),
        }


# Legacy ETA task name is retained to consume already-queued work safely.

@celery_app.task(
    bind=True,
    base=_BaseRelanceTask,
    name="app.tasks.relance.send_relance",
    queue="relance",
    acks_late=True,
)
def send_relance(self: Task, relance_id: str) -> dict:
    log.warning("relance.send.obsolete", relance_id=relance_id)
    return {"status": "obsolete", "skipped": True, "relance_id": relance_id}


@celery_app.task(
    name="app.tasks.relance.process_due",
    queue="relance",
    acks_late=True,
)
def process_due_relances() -> dict:
    """Deprecated compatibility task; relances are delivered by ETA tasks."""
    log.warning("relance.process_due.obsolete")
    return {"status": "obsolete", "skipped": True, "dispatched": 0}


# Backward-compatible task export expected by setup validation.
schedule_next_relance = scan_eligible_leads
