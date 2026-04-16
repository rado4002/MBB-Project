"""
EP-01: POST /api/v1/messages          — Production / Official WA API path
EP-01b: POST /api/v1/messages/baileys — Dev/Baileys bridge path
EP-02: POST /api/v1/messages/send     — Internal outbound dispatch

M1 Message Gateway router. Handles inbound WhatsApp messages, applies DRC
resilience rules (dedup, rate limit, blackout queue), and dispatches to the
M1 Celery worker for AI processing.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import DBSession, IdempotencyKey, get_current_role
from app.config import get_settings
from app.redis_client import blackout_enqueue
from app.redis_utils import dedup_check_and_mark, rate_limit_check
from app.schemas.messages import (
    InboundMessageRequest,
    QueuedMessageResponse,
)

log = structlog.get_logger()
settings = get_settings()
router = APIRouter(prefix="/messages", tags=["M1 — Gateway"])

_DRC_PHONE_RE = re.compile(r"^\+243[0-9]{9}$")


# ── Shared processing helper ───────────────────────────────────────────────────

async def _handle_inbound(
    *,
    payload: InboundMessageRequest,
    source: str = "official",
) -> QueuedMessageResponse:
    """
    Shared inbound processing:
      1. Dedup via whatsapp_message_id (Redis DB2, 24 h TTL, atomic SET NX)
      2. Rate limit: 10 messages/minute per customer (Redis DB2 counter)
      3. Dispatch Celery task or push to blackout queue on broker failure
    Returns QueuedMessageResponse (HTTP 202 semantics).
    """
    # ── 1. Dedup ──────────────────────────────────────────────────────────────
    if await dedup_check_and_mark(payload.whatsapp_message_id):
        log.info("m1.duplicate_ignored", wa_id=payload.whatsapp_message_id, source=source)
        return QueuedMessageResponse(queue_position=0, estimated_processing_seconds=0)

    # ── 2. Rate limit ──────────────────────────────────────────────────────────
    if await rate_limit_check(payload.customer_phone):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="rate_limit_exceeded",
        )

    # ── 3. Dispatch ───────────────────────────────────────────────────────────
    task_kwargs = {
        "message_id": str(payload.message_id),
        "customer_phone": payload.customer_phone,
        "content": payload.content,
        "content_type": payload.content_type.value,
        "timestamp": payload.timestamp.isoformat(),
        "whatsapp_message_id": payload.whatsapp_message_id,
    }

    from app.tasks.celery_app import celery_app

    try:
        task = celery_app.send_task(
            "m1.process_inbound_message",
            kwargs=task_kwargs,
            queue="default",
        )
        log.info(
            "m1.task_dispatched",
            task_id=task.id,
            wa_id=payload.whatsapp_message_id,
            source=source,
        )
        return QueuedMessageResponse(queue_position=1, estimated_processing_seconds=10)
    except Exception as exc:
        # Celery broker unreachable → push to blackout queue (AOF-persisted)
        log.warning("m1.celery_unavailable", error=str(exc), source=source)
        try:
            await blackout_enqueue(task_kwargs)
        except Exception as q_exc:
            log.error("m1.blackout_queue_failed", error=str(q_exc))
        return QueuedMessageResponse(queue_position=-1, estimated_processing_seconds=300)


# ── EP-01: Official / Production path ────────────────────────────────────────

@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=QueuedMessageResponse,
    responses={
        202: {"model": QueuedMessageResponse},
        400: {"description": "Validation error"},
        401: {"description": "Unauthorized"},
        429: {"description": "Rate limit exceeded"},
    },
    dependencies=[Depends(get_current_role)],
    summary="Receive inbound WhatsApp message (production path)",
)
async def receive_message(
    request: Request,
    payload: InboundMessageRequest,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """
    Accepts inbound messages from the WhatsApp Business API (production).

    Authentication: JWT bearer token.
    Returns 202 immediately — processing is async via Celery.
    """
    log.info(
        "m1.receive",
        wa_id=payload.whatsapp_message_id,
        phone=payload.customer_phone,
        content_type=payload.content_type,
        source="official",
    )
    return await _handle_inbound(payload=payload, source="official")


# ── EP-01b: Baileys dev/testing path ─────────────────────────────────────────

class BaileysWebhookPayload(BaseModel):
    """Normalized payload from the Baileys Node.js bridge."""
    message_id: uuid.UUID = Field(default_factory=uuid.uuid4)
    customer_phone: str = Field(..., description="DRC E.164 phone: +243XXXXXXXXX")
    content: str = Field(..., min_length=1, max_length=4096)
    content_type: str = Field(default="text")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    whatsapp_message_id: str = Field(..., max_length=100)

    @field_validator("customer_phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _DRC_PHONE_RE.match(v):
            raise ValueError("Phone must be DRC E.164 format: +243XXXXXXXXX")
        return v

    def to_inbound_request(self) -> InboundMessageRequest:
        from app.schemas.common import ContentType
        ct_map = {
            "text": ContentType.text,
            "voice_note": ContentType.voice_note,
            "image": ContentType.image,
        }
        return InboundMessageRequest(
            message_id=self.message_id,
            customer_phone=self.customer_phone,
            content=self.content,
            content_type=ct_map.get(self.content_type, ContentType.text),
            timestamp=self.timestamp,
            whatsapp_message_id=self.whatsapp_message_id,
        )


@router.post(
    "/baileys",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=QueuedMessageResponse,
    responses={
        202: {"model": QueuedMessageResponse},
        400: {"description": "Validation error"},
        401: {"description": "Invalid webhook secret"},
        429: {"description": "Rate limit exceeded"},
        503: {"description": "Baileys mode not active"},
    },
    summary="Receive inbound message from Baileys bridge (dev/testing)",
)
async def receive_from_baileys(
    request: Request,
    payload: BaileysWebhookPayload,
    x_webhook_secret: Annotated[str | None, Header()] = None,
):
    """
    Accepts inbound messages from the Baileys Node.js bridge (dev mode).

    Authentication: X-Webhook-Secret header (shared secret).
    Only active when `WHATSAPP_MODE=baileys`.
    Returns 202 immediately — processing is async via Celery.
    """
    if settings.whatsapp_mode != "baileys":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Baileys mode is not active (WHATSAPP_MODE=official)",
        )

    expected_secret = getattr(settings, "baileys_webhook_secret", "")
    if not expected_secret or x_webhook_secret != expected_secret:
        log.warning(
            "m1.baileys.invalid_secret",
            remote=request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid_webhook_secret",
        )

    log.info(
        "m1.baileys.receive",
        wa_id=payload.whatsapp_message_id,
        phone=payload.customer_phone,
        content_type=payload.content_type,
    )
    inbound = payload.to_inbound_request()
    return await _handle_inbound(payload=inbound, source="baileys")


# ── EP-02: Internal outbound send ────────────────────────────────────────────

class OutboundSendRequest(BaseModel):
    customer_phone: str = Field(..., description="DRC E.164 phone: +243XXXXXXXXX")
    text: str = Field(..., min_length=1, max_length=4096)
    conversation_id: uuid.UUID | None = None

    @field_validator("customer_phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _DRC_PHONE_RE.match(v):
            raise ValueError("Phone must be DRC E.164 format: +243XXXXXXXXX")
        return v


class OutboundSendResponse(BaseModel):
    status: str
    provider_message_id: str = ""


@router.post(
    "/send",
    status_code=status.HTTP_200_OK,
    response_model=OutboundSendResponse,
    responses={
        200: {"model": OutboundSendResponse},
        401: {"description": "Unauthorized"},
        502: {"description": "Messaging provider unreachable"},
    },
    dependencies=[Depends(get_current_role)],
    summary="Send an outbound message (internal — M4/M5/M6 use this)",
)
async def send_outbound(
    payload: OutboundSendRequest,
):
    """
    Internal endpoint for other modules (M4 nurturing, M5 relance, M6 conversion)
    to dispatch outbound messages without importing the adapter directly.
    """
    from app.adapters import get_messaging_adapter

    log.info(
        "m1.outbound.send",
        phone=payload.customer_phone,
        conv_id=str(payload.conversation_id) if payload.conversation_id else None,
    )
    try:
        adapter = get_messaging_adapter()
        provider_id = await adapter.send_message(payload.customer_phone, payload.text)
        return OutboundSendResponse(status="sent", provider_message_id=provider_id)
    except Exception as exc:
        log.error("m1.outbound.failed", phone=payload.customer_phone, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"messaging_provider_error: {exc}",
        )
