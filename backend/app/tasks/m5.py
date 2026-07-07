"""
app/tasks/m5.py — Celery tasks for M5 Lead Qualification.

Tasks:
  - sync_lead_to_crm : Sync a qualified lead to the CRM adapter (Airtable)
"""
from __future__ import annotations

import structlog
from celery import Task

from app.tasks.celery_app import celery_app, run_async

log = structlog.get_logger(__name__)


class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 60


@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="m5.sync_lead_to_crm",
    queue="default",
    acks_late=True,
)
def sync_lead_to_crm(
    self: Task,
    *,
    lead_id: str,
    customer_phone: str,
    score: str,
    score_value: int,
    intent: str,
) -> dict:
    """Sync a lead to the CRM adapter (Airtable). Retries on failure."""
    return run_async(
        _sync(
            task=self,
            lead_id=lead_id,
            customer_phone=customer_phone,
            score=score,
            score_value=score_value,
            intent=intent,
        )
    )


async def _sync(
    *,
    task: Task,
    lead_id: str,
    customer_phone: str,
    score: str,
    score_value: int,
    intent: str,
) -> dict:
    from app.adapters import get_crm_adapter

    try:
        crm = get_crm_adapter()
        record_id = await crm.create_lead({
            "lead_id": lead_id,
            "customer_phone": customer_phone,
            "score": score,
            "score_value": score_value,
            "intent": intent,
            "source": "whatsapp_auto",
        })
        log.info(
            "m5.crm_sync.success",
            lead_id=lead_id,
            crm_record_id=record_id,
        )
        return {"status": "synced", "lead_id": lead_id, "crm_record_id": record_id}
    except Exception as exc:
        log.error("m5.crm_sync.failed", lead_id=lead_id, error=str(exc))
        raise task.retry(exc=exc, countdown=2 ** task.request.retries * 60)
