"""
EP-08: POST /api/v1/relances
EP-09: PUT  /api/v1/relances/{relance_id}/delivered
EP-10: PUT  /api/v1/relances/{relance_id}/response
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DBSession, IdempotencyKey, get_current_role
from app.schemas.relances import (
    RelanceCreate,
    RelanceDeliverResponse,
    RelanceDeliverUpdate,
    RelanceResponse,
    RelanceResponseUpdate,
    RelanceResponseUpdateResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/relances", tags=["M6 — Relance Engine"])


@router.post(
    "",
    response_model=RelanceResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_role)],
)
async def schedule_relance(
    body: RelanceCreate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """
    Schedule a relance message for a lead.
    Max 3 relances per lead; no messages 22:00–07:00 Kinshasa time.
    """
    log.info("relance.schedule", lead_id=str(body.lead_id), attempt=body.attempt_number)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M6 not yet implemented")


@router.put(
    "/{relance_id}/delivered",
    response_model=RelanceDeliverResponse,
    dependencies=[Depends(get_current_role)],
)
async def mark_delivered(
    relance_id: uuid.UUID,
    body: RelanceDeliverUpdate,
    db: DBSession,
):
    """Mark a relance as delivered (called by Celery task after send)."""
    log.info("relance.delivered", relance_id=str(relance_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M6 not yet implemented")


@router.put(
    "/{relance_id}/response",
    response_model=RelanceResponseUpdateResponse,
    dependencies=[Depends(get_current_role)],
)
async def record_response(
    relance_id: uuid.UUID,
    body: RelanceResponseUpdate,
    db: DBSession,
):
    """Record that a customer responded to a relance message."""
    log.info("relance.response", relance_id=str(relance_id), received=body.response_received)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M6 not yet implemented")
