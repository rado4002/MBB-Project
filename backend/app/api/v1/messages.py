"""
EP-01: POST /api/v1/messages
Receives inbound WhatsApp messages (from Baileys bridge or WhatsApp Business API).
Delegates processing to M1 → M2 → M3 via Celery task (async) or synchronous path.
"""
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Request, status

from app.api.deps import DBSession, IdempotencyKey, RedisClient, get_current_role
from app.schemas.messages import (
    InboundMessageRequest,
    InboundMessageResponse,
    QueuedMessageResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/messages", tags=["M1 — Gateway"])


@router.post(
    "",
    status_code=status.HTTP_200_OK,
    responses={
        200: {"model": InboundMessageResponse},
        202: {"model": QueuedMessageResponse},
        400: {"description": "Validation error"},
        401: {"description": "Unauthorized"},
        429: {"description": "Rate limit exceeded"},
        502: {"description": "Claude LLM unreachable"},
    },
    dependencies=[Depends(get_current_role)],
)
async def receive_message(
    request: Request,
    payload: InboundMessageRequest,
    db: DBSession,
    redis: RedisClient,
    idempotency_key: IdempotencyKey,
):
    """
    Process an inbound WhatsApp message.

    - Validates DRC phone number and timestamp.
    - Deduplicates via whatsapp_message_id (Redis TTL 24 h).
    - Routes voice notes straight to M8 escalation (no AI processing of audio).
    - Returns 200 when processed synchronously, 202 when queued (blackout/load).
    """
    log.info(
        "message.received",
        wa_id=payload.whatsapp_message_id,
        phone=payload.customer_phone,
        content_type=payload.content_type,
    )

    # Dedup check — idempotent: return 202 silently for replays
    dedup_key = f"msg:dedup:{payload.whatsapp_message_id}"
    already_processed = await redis.get(dedup_key)
    if already_processed:
        log.info("message.duplicate_ignored", wa_id=payload.whatsapp_message_id)
        return QueuedMessageResponse(queue_position=0, estimated_processing_seconds=0)

    # Rate limit: max 10 messages/minute per customer
    rate_key = f"ratelimit:msg:{payload.customer_phone}"
    count = await redis.incr(rate_key)
    if count == 1:
        await redis.expire(rate_key, 60)
    if count > 10:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limit_exceeded",
        )

    # Mark dedup (24 h TTL)
    await redis.setex(dedup_key, 86400, "1")

    # Dispatch to M1 Celery task — returns 202 immediately
    from app.tasks.celery_app import celery_app  # lazy import — avoid circular

    task = celery_app.send_task(
        "m1.process_inbound_message",
        kwargs={
            "message_id": str(payload.message_id),
            "customer_phone": payload.customer_phone,
            "content": payload.content,
            "content_type": payload.content_type.value,
            "timestamp": payload.timestamp.isoformat(),
            "whatsapp_message_id": payload.whatsapp_message_id,
        },
        queue="default",
    )

    log.info("message.queued", task_id=task.id, wa_id=payload.whatsapp_message_id)
    return QueuedMessageResponse(queue_position=1, estimated_processing_seconds=10)
