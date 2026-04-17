"""
EP-08: POST /api/v1/relances
EP-09: PUT  /api/v1/relances/{relance_id}/delivered
EP-10: PUT  /api/v1/relances/{relance_id}/response
A-11:  GET  /api/v1/relances (admin list)
"""
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.relance import Relance
from app.modules.m6_relance.service import create_and_schedule_relance
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
    Idempotent via unique constraint (lead_id, attempt_number).
    """
    log.info("relance.schedule", lead_id=str(body.lead_id), attempt=body.attempt_number)
    
    # Check if relance already exists for this lead + attempt (idempotency)
    existing = await db.execute(
        select(Relance).where(
            Relance.lead_id == body.lead_id,
            Relance.attempt_number == body.attempt_number,
        )
    )
    if (existing_relance := existing.scalar_one_or_none()) is not None:
        log.debug("relance.already_exists", relance_id=str(existing_relance.relance_id))
        return RelanceResponse(
            relance_id=existing_relance.relance_id,
            lead_id=existing_relance.lead_id,
            attempt_number=existing_relance.attempt_number,
            scheduled_at=existing_relance.scheduled_at,
            delivered_at=existing_relance.delivered_at,
            response_received=existing_relance.response_received,
            value_hook=existing_relance.value_hook,
            hook_type=existing_relance.hook_type,
            cancelled=existing_relance.cancelled,
            created_at=existing_relance.created_at,
        )
    
    # Fetch lead and conversation
    lead = await db.get(Lead, body.lead_id, options=[selectinload(Lead.relances)])
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    
    if lead.relance_count >= 3:
        raise HTTPException(status_code=400, detail="Max relances reached (3)")
    
    conv = await db.get(Conversation, lead.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Create relance via service
    relance = await create_and_schedule_relance(
        session=db,
        lead=lead,
        conversation=conv,
    )
    
    if not relance:
        raise HTTPException(status_code=400, detail="Could not schedule relance")
    
    await db.commit()
    
    return RelanceResponse(
        relance_id=relance.relance_id,
        lead_id=relance.lead_id,
        attempt_number=relance.attempt_number,
        scheduled_at=relance.scheduled_at,
        delivered_at=relance.delivered_at,
        response_received=relance.response_received,
        value_hook=relance.value_hook,
        hook_type=relance.hook_type,
        cancelled=relance.cancelled,
        created_at=relance.created_at,
    )


@router.put(
    "/{relance_id}/delivered",
    response_model=RelanceDeliverResponse,
    dependencies=[Depends(get_current_role)],
)
async def mark_delivered(
    relance_id: uuid.UUID,
    body: RelanceDeliverUpdate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Mark a relance as delivered (called by Celery task after send). Idempotent via relance_id PK."""
    log.info("relance.delivered", relance_id=str(relance_id))
    
    relance = await db.get(Relance, relance_id)
    if not relance:
        raise HTTPException(status_code=404, detail="Relance not found")
    
    # Idempotency: if already delivered, return success
    if relance.delivered_at:
        log.debug("relance.already_delivered", relance_id=str(relance_id))
        return RelanceDeliverResponse(
            relance_id=relance.relance_id,
            delivered_at=relance.delivered_at,
            status="delivered",
        )
    
    # Mark as delivered
    relance.delivered_at = body.delivered_at or datetime.now(timezone.utc)
    await db.commit()
    
    log.info("relance.marked_delivered", relance_id=str(relance_id))
    
    return RelanceDeliverResponse(
        relance_id=relance.relance_id,
        delivered_at=relance.delivered_at,
        status="delivered",
    )


@router.put(
    "/{relance_id}/response",
    response_model=RelanceResponseUpdateResponse,
    dependencies=[Depends(get_current_role)],
)
async def record_response(
    relance_id: uuid.UUID,
    body: RelanceResponseUpdate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Record that a customer responded to a relance message. Idempotent via relance_id PK."""
    log.info("relance.response", relance_id=str(relance_id), received=body.response_received)
    
    relance = await db.get(Relance, relance_id)
    if not relance:
        raise HTTPException(status_code=404, detail="Relance not found")
    
    # Update response status (idempotent: overwriting is safe)
    relance.response_received = body.response_received
    
    # If customer responded, cancel remaining relances for this lead
    if body.response_received:
        from app.modules.m6_relance.service import cancel_all_relances
        cancelled_count = await cancel_all_relances(
            session=db,
            lead_id=relance.lead_id,
        )
        log.info(
            "relance.cancelled_remaining",
            lead_id=str(relance.lead_id),
            count=cancelled_count,
        )
    
    await db.commit()
    
    return RelanceResponseUpdateResponse(
        relance_id=relance.relance_id,
        response_received=relance.response_received,
        updated_at=datetime.now(timezone.utc),
    )


@router.get(
    "",
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def list_relances(
    db: DBSession,
    lead_id: uuid.UUID | None = Query(default=None),
    attempt: int | None = Query(default=None, ge=1, le=3),
    delivered: bool | None = Query(default=None),
    responded: bool | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Admin: list all relances with filters (A-11)."""
    log.info("relance.list", lead_id=lead_id, attempt=attempt, limit=limit, offset=offset)
    
    # Build query with filters
    q = select(Relance).order_by(Relance.scheduled_at.desc())
    count_q = select(func.count()).select_from(Relance)
    
    if lead_id:
        q = q.where(Relance.lead_id == lead_id)
        count_q = count_q.where(Relance.lead_id == lead_id)
    
    if attempt:
        q = q.where(Relance.attempt_number == attempt)
        count_q = count_q.where(Relance.attempt_number == attempt)
    
    if delivered is not None:
        if delivered:
            q = q.where(Relance.delivered_at.isnot(None))
            count_q = count_q.where(Relance.delivered_at.isnot(None))
        else:
            q = q.where(Relance.delivered_at.is_(None))
            count_q = count_q.where(Relance.delivered_at.is_(None))
    
    if responded is not None:
        q = q.where(Relance.response_received == responded)
        count_q = count_q.where(Relance.response_received == responded)
    
    # Execute queries
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(
        q.offset(offset).limit(limit).options(selectinload(Relance.lead))
    )
    relances = result.scalars().all()
    
    # Format response
    items = []
    for r in relances:
        items.append({
            "relance_id": str(r.relance_id),
            "lead_id": str(r.lead_id),
            "customer_id": r.lead.customer_id if r.lead else None,
            "attempt_number": r.attempt_number,
            "scheduled_at": r.scheduled_at.isoformat(),
            "delivered_at": r.delivered_at.isoformat() if r.delivered_at else None,
            "response_received": r.response_received,
            "value_hook": r.value_hook[:100] + "..." if len(r.value_hook) > 100 else r.value_hook,
            "hook_type": r.hook_type,
            "cancelled": r.cancelled,
            "created_at": r.created_at.isoformat(),
        })
    
    return {
        "relances": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
