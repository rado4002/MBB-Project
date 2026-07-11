"""
EP-01: POST /api/v1/messages          — Production / Official WA API path
EP-01b: POST /api/v1/messages/baileys — Dev/Baileys bridge path
EP-01c: POST /api/v1/messages/webhook — Universal webhook (dual-mode)
EP-01d: GET  /api/v1/messages/webhook — Webhook verification (Official API)
EP-02: POST /api/v1/messages/send     — Internal outbound dispatch

M1 Message Gateway router. Handles inbound WhatsApp messages, applies DRC
resilience rules (dedup, rate limit, blackout queue), and dispatches to the
M1 Celery worker for AI processing.
"""
from __future__ import annotations

import hashlib
import hmac
import re
import uuid
from datetime import datetime, timezone
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, field_validator

from app.api.deps import DBSession, IdempotencyKey, get_current_role
from app.config import get_settings
from app.modules.m1_gateway.normalizer import normalize_webhook
from app.redis_client import blackout_enqueue
from app.redis_utils import dedup_check_and_mark, rate_limit_check
from app.schemas.messages import (
    InboundMessageRequest,
    MessageHistoryResponse,
    QueuedMessageResponse,
    validate_whatsapp_message_id_value,
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
    whatsapp_message_id: str = Field(..., min_length=1, max_length=100)

    @field_validator("customer_phone")
    @classmethod
    def validate_phone(cls, v: str) -> str:
        if not _DRC_PHONE_RE.match(v):
            raise ValueError("Phone must be DRC E.164 format: +243XXXXXXXXX")
        return v

    @field_validator("whatsapp_message_id")
    @classmethod
    def validate_whatsapp_message_id(cls, v: str) -> str:
        return validate_whatsapp_message_id_value(v)

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


# ── EP-01c: Universal webhook (dual-mode) ────────────────────────────────────

@router.get(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Webhook verification (Official WhatsApp API)",
)
async def verify_webhook(
    hub_mode: Annotated[str | None, Query(alias="hub.mode")] = None,
    hub_challenge: Annotated[str | None, Query(alias="hub.challenge")] = None,
    hub_verify_token: Annotated[str | None, Query(alias="hub.verify_token")] = None,
):
    """
    Webhook verification endpoint for Official WhatsApp Business API.

    When Meta sets up the webhook, it sends a GET request with:
      - hub.mode=subscribe
      - hub.verify_token=<your_token>
      - hub.challenge=<random_string>

    You must respond with the challenge string to verify ownership.
    """
    expected_token = getattr(settings, "whatsapp_verify_token", "mbb_webhook_verify_2026")

    if hub_mode == "subscribe" and hub_verify_token == expected_token:
        log.info("webhook.verified", mode=hub_mode)
        return int(hub_challenge) if hub_challenge and hub_challenge.isdigit() else hub_challenge
    else:
        log.warning("webhook.verification_failed", mode=hub_mode, token_match=(hub_verify_token == expected_token))
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="verification_failed")


@router.post(
    "/webhook",
    status_code=status.HTTP_200_OK,
    summary="Universal webhook for WhatsApp (dual-mode: Baileys or Official API)",
)
async def receive_webhook(
    request: Request,
    x_hub_signature_256: Annotated[str | None, Header()] = None,
    x_webhook_secret: Annotated[str | None, Header()] = None,
):
    """
    Universal webhook endpoint that handles both Baileys and Official API formats.

    Mode detection:
      - Baileys: X-Webhook-Secret header present
      - Official API: X-Hub-Signature-256 header present (HMAC-SHA256)

    Returns 200 immediately (WhatsApp expects 200, not 202).
    Processing is async via Celery.
    """
    body_bytes = await request.body()

    # Detect mode based on headers
    is_baileys = x_webhook_secret is not None
    is_official = x_hub_signature_256 is not None

    if is_baileys:
        # Baileys mode: verify shared secret
        expected_secret = getattr(settings, "baileys_webhook_secret", "")
        if not expected_secret or x_webhook_secret != expected_secret:
            log.warning("webhook.baileys.invalid_secret", remote=request.client.host if request.client else "unknown")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_webhook_secret")

        # Parse and normalize Baileys payload
        import json
        try:
            payload_dict = json.loads(body_bytes)
            normalized = normalize_webhook(payload_dict, source="baileys")
        except Exception as exc:
            log.error("webhook.baileys.parse_error", error=str(exc))
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    elif is_official:
        # Official API mode: verify HMAC-SHA256 signature
        expected_secret = getattr(settings, "whatsapp_api_secret", "")
        if not expected_secret:
            log.error("webhook.official.no_secret_configured")
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="webhook_secret_not_configured")

        # Verify signature
        signature = x_hub_signature_256.replace("sha256=", "") if x_hub_signature_256 else ""
        expected_signature = hmac.new(
            expected_secret.encode(),
            body_bytes,
            hashlib.sha256,
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            log.warning("webhook.official.invalid_signature", remote=request.client.host if request.client else "unknown")
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid_signature")

        # Parse and normalize Official API payload
        import json
        try:
            payload_dict = json.loads(body_bytes)
            normalized = normalize_webhook(payload_dict, source="official")
        except Exception as exc:
            log.error("webhook.official.parse_error", error=str(exc))
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    else:
        # No auth header found
        log.warning("webhook.no_auth_header", remote=request.client.host if request.client else "unknown")
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing_auth_header")

    # Convert normalized dict to InboundMessageRequest
    try:
        from app.schemas.common import ContentType
        inbound = InboundMessageRequest(
            message_id=uuid.UUID(normalized["message_id"]),
            customer_phone=normalized["customer_phone"],
            content=normalized["content"],
            content_type=ContentType(normalized["content_type"]),
            timestamp=datetime.fromisoformat(normalized["timestamp"]),
            whatsapp_message_id=normalized["whatsapp_message_id"],
        )
    except Exception as exc:
        log.error("webhook.normalization_error", error=str(exc), normalized=normalized)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"normalization_error: {exc}") from exc

    # Dispatch to M1 processing
    source = "baileys" if is_baileys else "official"
    log.info(
        "webhook.received",
        wa_id=inbound.whatsapp_message_id,
        phone=inbound.customer_phone,
        content_type=inbound.content_type,
        source=source,
    )

    await _handle_inbound(payload=inbound, source=source)

    # WhatsApp expects 200 OK (not 202)
    return {"status": "received"}


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


# ── EP-03: Message history ───────────────────────────────────────────────────

@router.get(
    "/{conversation_id}",
    status_code=status.HTTP_200_OK,
    response_model=MessageHistoryResponse,
    responses={
        200: {"model": MessageHistoryResponse},
        401: {"description": "Unauthorized"},
        404: {"description": "Conversation not found"},
    },
    dependencies=[Depends(get_current_role)],
    summary="Retrieve message history for a conversation",
)
async def get_message_history(
    conversation_id: uuid.UUID,
    db: DBSession,
    limit: int = 50,
    offset: int = 0,
):
    """
    Return paginated message history for a conversation.
    Ordered by timestamp ascending (oldest first).
    """
    from sqlalchemy import func, select

    from app.models.conversation import Conversation
    from app.models.message import Message

    # Verify conversation exists
    conv_result = await db.execute(
        select(Conversation).where(
            Conversation.conversation_id == conversation_id
        )
    )
    if conv_result.scalar_one_or_none() is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="conversation_not_found",
        )

    # Count total messages
    count_result = await db.execute(
        select(func.count()).where(Message.conversation_id == conversation_id)
    )
    total = count_result.scalar() or 0

    # Fetch paginated messages
    limit = min(limit, 100)  # cap at 100
    msg_result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.timestamp.asc())
        .offset(offset)
        .limit(limit)
    )
    messages = msg_result.scalars().all()

    return MessageHistoryResponse(
        conversation_id=conversation_id,
        messages=messages,
        total=total,
    )
