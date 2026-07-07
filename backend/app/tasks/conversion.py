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

from app.tasks.celery_app import celery_app, run_async

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
    from app.modules.m7_conversion import service as conversion_svc  # type: ignore[import]

    log.info("conversion.initiate_payment.start", order_id=order_id)
    try:
        result = run_async(
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
    name="m7.process_payment_callback",
    queue="conversion",
    acks_late=True,
)
def process_payment_callback(
    self: Task,
    transaction_id: str,
    order_id: str,
    provider: str,
    status: str,
    amount_cdf: str,
    customer_phone: str,
    timestamp: str,
) -> dict:
    """
    Handle a Mobile Money provider callback.

    Idempotent: re-delivering the same callback (same transaction_id + provider)
    returns the existing result without double-processing.

    Args:
        transaction_id:   Provider-assigned payment reference.
        order_id:         UUID of Order record.
        provider:         "orange" | "airtel" | "mpesa".
        status:           "success" | "failed" | "pending".
        amount_cdf:       Payment amount in CDF.
        customer_phone:   Customer's phone number.
        timestamp:        Payment timestamp (ISO format).
    """
    log.info(
        "conversion.callback.start",
        transaction_id=transaction_id,
        order_id=order_id,
        provider=provider,
        status=status,
    )
    try:
        result = run_async(
            _process_payment_callback(
                transaction_id=transaction_id,
                order_id=order_id,
                provider=provider,
                status=status,
                amount_cdf=amount_cdf,
                customer_phone=customer_phone,
                timestamp=timestamp,
            )
        )
        log.info("conversion.callback.done", transaction_id=transaction_id, result=result)
        return result
    except Exception as exc:
        log.error("conversion.callback.error", transaction_id=transaction_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)


async def _process_payment_callback(
    transaction_id: str,
    order_id: str,
    provider: str,
    status: str,
    amount_cdf: str,
    customer_phone: str,
    timestamp: str,
) -> dict:
    """
    Core logic for processing payment callback.

    Delegates to service.process_callback() which handles all DB logic correctly.

    Returns:
        Dict with processing status
    """
    from app.modules.m7_conversion import service as conversion_svc  # type: ignore[import]

    # Use the correct service implementation instead of broken inline logic
    result = await conversion_svc.process_callback(
        payment_id=transaction_id,
        provider=provider,
        status=status,
        idempotency_key=f"{transaction_id}:{provider}",
    )

    # Send customer notification via WhatsApp
    from app.adapters.messaging import get_messaging_adapter

    messaging = get_messaging_adapter()
    if status == "success":
        await messaging.send_message(
            phone=customer_phone,
            text=f"Paiement reçu ✅ Commande #{order_id[:8]} confirmée. Livraison en cours!",
        )
    else:
        await messaging.send_message(
            phone=customer_phone,
            text=f"Paiement échoué ❌ Commande #{order_id[:8]}. Veuillez réessayer.",
        )

    return result


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
    from app.redis_client import blackout_dequeue_batch, blackout_queue_length
    from app.modules.m1_gateway import service as gateway_svc  # type: ignore[import]

    queue_len = run_async(blackout_queue_length())
    if queue_len <= 0:
        return {"drained": 0, "queue_was_empty": True}

    log.info("blackout.drain.start", queue_length=queue_len)
    messages = run_async(
        blackout_dequeue_batch(batch_size=50)
    )
    processed = 0
    for msg in messages:
        try:
            run_async(
                gateway_svc.reprocess_blackout_message(msg)
            )
            processed += 1
        except Exception as exc:  # noqa: BLE001
            log.error("blackout.drain.reprocess_failed", wa_id=msg.get("wa_id"), error=str(exc))

    log.info("blackout.drain.done", processed=processed, total=len(messages))
    return {"drained": processed, "total": len(messages)}


# ── Task: sync confirmed order to CRM ─────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.conversion.sync_order_crm",
    queue="conversion",
    acks_late=True,
)
def sync_order_crm(self: Task, *, order_id: str, idempotency_key: str) -> dict:
    """
    Sync a confirmed order to the Airtable CRM.

    Idempotent: safe to retry. If already synced, returns early.
    Target latency: < 2 minutes after order confirmation.
    """
    from app.modules.m7_conversion import service as conversion_svc  # type: ignore[import]

    log.info("conversion.crm_sync.start", order_id=order_id)
    try:
        crm_id = run_async(
            conversion_svc.sync_order_to_crm(
                order_id=order_id,
                idempotency_key=idempotency_key,
            )
        )
        log.info("conversion.crm_sync.done", order_id=order_id, crm_id=crm_id)
        return {"crm_id": crm_id, "order_id": order_id}
    except Exception as exc:
        log.error("conversion.crm_sync.error", order_id=order_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)


# ── Task: update order status ─────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.conversion.update_order_status",
    queue="conversion",
    acks_late=True,
)
def update_order_status(
    self: Task,
    *,
    order_id: str,
    new_status: str,
    idempotency_key: str,
) -> dict:
    """
    Advance an order through the state machine.

    Valid transitions:
      pending → confirmed → preparing → delivering → delivered
      Any (except delivered) → cancelled
    """
    import uuid as _uuid

    from app.modules.m7_conversion import service as conversion_svc  # type: ignore[import]

    log.info("conversion.status_update.start", order_id=order_id, new_status=new_status)
    try:
        async def _run():
            from app.database import AsyncSessionLocal
            async with AsyncSessionLocal() as session:
                return await conversion_svc.update_order_status(
                    session, order_id=_uuid.UUID(order_id), new_status=new_status
                )

        order = run_async(_run())
        log.info("conversion.status_update.done", order_id=order_id, status=order.status)
        return {"order_id": order_id, "status": order.status}
    except Exception as exc:
        log.error("conversion.status_update.error", order_id=order_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)

