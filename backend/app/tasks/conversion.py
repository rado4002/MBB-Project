"""
app/tasks/conversion.py — Celery tasks for M6 Conversion Engine.

Handles:
  - Initiating Mobile Money payment requests (Orange / Airtel / M-Pesa)
  - Processing payment callbacks (idempotent — webhook may replay)
  - Blackout queue recovery: drain and re-process messages queued during outages
  - Order status updates
"""

from __future__ import annotations

import structlog
from celery import Task

from app.tasks.celery_app import celery_app

log = structlog.get_logger(__name__)


class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 60


# ── Task: initiate a Mobile Money payment request ─────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.conversion.initiate_payment",
    queue="conversion",
    acks_late=True,
)
def initiate_payment(
    self: Task,
    order_id: str,
    idempotency_key: str,
) -> dict:
    """
    Call the MobileMoneyAdapter to initiate payment for *order_id*.

    Args:
        order_id:         UUID of the Order row.
        idempotency_key:  Client-supplied key — safe to retry.

    Returns dict with {"status": "pending" | "failed", "payment_id": str}.
    """
    import asyncio

    from app.modules.m7_conversion import service as conversion_svc  # type: ignore[import]

    log.info("conversion.initiate_payment.start", order_id=order_id)
    try:
        result = asyncio.get_event_loop().run_until_complete(
            conversion_svc.initiate_payment(
                order_id=order_id,
                idempotency_key=idempotency_key,
            )
        )
        log.info("conversion.initiate_payment.done", order_id=order_id, status=result.get("status"))
        return result
    except Exception as exc:
        log.error("conversion.initiate_payment.error", order_id=order_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


# ── Task: process a payment callback ─────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.conversion.process_payment_callback",
    queue="conversion",
    acks_late=True,
)
def process_payment_callback(
    self: Task,
    payment_id: str,
    provider: str,
    status: str,
    idempotency_key: str,
) -> dict:
    """
    Handle a Mobile Money provider callback.

    Idempotent: re-delivering the same callback (same idempotency_key)
    returns the existing result without double-processing.

    Args:
        payment_id:       Provider-assigned payment reference.
        provider:         "orange" | "airtel" | "mpesa".
        status:           "success" | "failed" | "pending".
        idempotency_key:  Deduplication key (from webhook header or payload).
    """
    import asyncio

    from app.modules.m7_conversion import service as conversion_svc  # type: ignore[import]

    log.info("conversion.callback.start", payment_id=payment_id, provider=provider, status=status)
    try:
        result = asyncio.get_event_loop().run_until_complete(
            conversion_svc.process_callback(
                payment_id=payment_id,
                provider=provider,
                status=status,
                idempotency_key=idempotency_key,
            )
        )
        log.info("conversion.callback.done", payment_id=payment_id, result=result)
        return result
    except Exception as exc:
        log.error("conversion.callback.error", payment_id=payment_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)


# ── Task: drain the blackout queue (Beat-triggered + on-startup) ──────────────

@celery_app.task(
    name="app.tasks.conversion.drain_blackout_queue",
    queue="default",
    acks_late=True,
)
def drain_blackout_queue() -> dict:
    """
    Periodic beat task (runs every 5 min).

    Drains the Redis blackout queue (DB 3) and re-submits each message to
    the M1 Gateway ingest endpoint so it is processed normally.

    On recovery after a Kinshasa power outage, each re-processed message
    triggers the standard conversation flow, then sends the DRC recovery
    confirmation: "Naza-zonga! Message na yo e-batelami ✓"
    """
    import asyncio

    from app.redis_client import blackout_dequeue_batch, blackout_queue_length
    from app.modules.m1_gateway import service as gateway_svc  # type: ignore[import]

    queue_len = asyncio.get_event_loop().run_until_complete(blackout_queue_length())
    if queue_len <= 0:
        return {"drained": 0, "queue_was_empty": True}

    log.info("blackout.drain.start", queue_length=queue_len)
    messages = asyncio.get_event_loop().run_until_complete(
        blackout_dequeue_batch(batch_size=50)
    )
    processed = 0
    for msg in messages:
        try:
            asyncio.get_event_loop().run_until_complete(
                gateway_svc.reprocess_blackout_message(msg)
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            log.error("blackout.drain.reprocess_failed", wa_id=msg.get("wa_id"), error=str(exc))

    log.info("blackout.drain.done", processed=processed, total=len(messages))
    return {"drained": processed, "total": len(messages)}
