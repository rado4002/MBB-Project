"""
EP-04: POST /api/v1/leads
EP-05: GET  /api/v1/leads/{lead_id}
EP-06: PUT  /api/v1/leads/{lead_id}/score
EP-07: PUT  /api/v1/leads/{lead_id}/stage
A-08:  GET  /api/v1/leads  (admin list)
"""
import uuid
from datetime import datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.modules.m5_qualification.service import qualify_and_create_lead
from app.schemas.leads import (
    CustomerInLead,
    LeadCreate,
    LeadCreatedResponse,
    LeadResponse,
    LeadScoreResponse,
    LeadScoreUpdate,
    LeadStageResponse,
    LeadStageUpdate,
    RelanceSummary,
)

log = structlog.get_logger()
router = APIRouter(prefix="/leads", tags=["M5 — Lead Qualification"])


@router.post(
    "",
    response_model=LeadCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_role)],
)
async def create_lead(
    body: LeadCreate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Qualify a conversation into a lead (called internally after qualification flow). Idempotent via unique constraint."""
    log.info("lead.create", conversation_id=str(body.conversation_id))
    
    # Check if lead already exists (idempotency via unique constraint)
    existing = await db.execute(
        select(Lead).where(Lead.conversation_id == body.conversation_id)
    )
    if (existing_lead := existing.scalar_one_or_none()) is not None:
        log.debug("lead.already_exists", lead_id=str(existing_lead.lead_id))
        return LeadCreatedResponse(
            lead_id=existing_lead.lead_id,
            customer_id=existing_lead.customer_id,
            conversation_id=existing_lead.conversation_id,
            score=existing_lead.score,
            score_value=existing_lead.score_value,
            stage=existing_lead.stage,
            created_at=existing_lead.created_at,
        )
    
    # Fetch conversation to get customer phone
    conv = await db.get(Conversation, body.conversation_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")
    
    # Create lead via service layer
    lead = await qualify_and_create_lead(
        session=db,
        customer_phone=conv.customer_id,
        conversation_id=body.conversation_id,
        message_text=body.message_text or "",
        msg_count=conv.message_count,
        response_speed_s=body.response_speed_s,
    )
    
    if not lead:
        raise HTTPException(
            status_code=400,
            detail="Lead does not meet qualification threshold (score < 40)",
        )
    
    await db.commit()
    
    return LeadCreatedResponse(
        lead_id=lead.lead_id,
        customer_id=lead.customer_id,
        conversation_id=lead.conversation_id,
        score=lead.score,
        score_value=lead.score_value,
        stage=lead.stage,
        created_at=lead.created_at,
    )


@router.get(
    "/{lead_id}",
    response_model=LeadResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_lead(lead_id: uuid.UUID, db: DBSession):
    """Retrieve lead details with relance history."""
    log.info("lead.get", lead_id=str(lead_id))
    
    lead = await db.get(Lead, lead_id, options=[
        selectinload(Lead.relances),
    ])
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    
    # Fetch customer name for display
    customer = await db.get(Customer, lead.customer_id)
    
    return LeadResponse(
        lead_id=lead.lead_id,
        customer=CustomerInLead(
            phone_number=lead.customer_id,
            name=customer.name if customer else None,
            city=customer.city if customer else None,
        ),
        score=lead.score,
        score_value=lead.score_value,
        stage=lead.stage,
        intent=lead.intent,
        product_interest=lead.product_interest or [],
        source=lead.source,
        relance_count=lead.relance_count,
        relances=[
            RelanceSummary(
                relance_id=r.relance_id,
                attempt_number=r.attempt_number,
                scheduled_at=r.scheduled_at,
                delivered_at=r.delivered_at,
                response_received=r.response_received,
                hook_type=r.hook_type,
            )
            for r in lead.relances
        ],
        qualified_at=lead.qualified_at,
        converted_at=lead.converted_at,
    )


@router.put(
    "/{lead_id}/score",
    response_model=LeadScoreResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_lead_score(lead_id: uuid.UUID, body: LeadScoreUpdate, db: DBSession, idempotency_key: IdempotencyKey):
    """Update lead score as new signals arrive. Idempotent via lead_id PK."""
    log.info("lead.score.update", lead_id=str(lead_id), score_value=body.score_value)
    
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    
    # Update score
    lead.score_value = body.score_value
    
    # Update label based on value (hot ≥70, warm 40-69, cold <40)
    if body.score_value >= 70:
        lead.score = "hot"
    elif body.score_value >= 40:
        lead.score = "warm"
    else:
        lead.score = "cold"
    
    # Auto-advance stage if score increased significantly
    if body.score_value >= 70 and lead.stage == "awareness":
        lead.stage = "decision"
        log.info("lead.stage_auto_advanced", lead_id=str(lead_id), new_stage="decision")
    elif body.score_value >= 55 and lead.stage == "awareness":
        lead.stage = "consideration"
        log.info("lead.stage_auto_advanced", lead_id=str(lead_id), new_stage="consideration")
    
    lead.updated_at = datetime.now(timezone.utc)
    await db.commit()
    
    return LeadScoreResponse(
        lead_id=lead.lead_id,
        score=lead.score,
        score_value=lead.score_value,
        stage=lead.stage,
        updated_at=lead.updated_at,
    )


@router.put(
    "/{lead_id}/stage",
    response_model=LeadStageResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_lead_stage(lead_id: uuid.UUID, body: LeadStageUpdate, db: DBSession, idempotency_key: IdempotencyKey):
    """Advance lead through awareness → consideration → decision funnel. Idempotent via lead_id PK."""
    log.info("lead.stage.update", lead_id=str(lead_id), stage=body.stage)
    
    lead = await db.get(Lead, lead_id)
    if not lead:
        raise HTTPException(status_code=404, detail="Lead not found")
    
    # Validate stage progression (awareness → consideration → decision)
    valid_stages = {"awareness", "consideration", "decision"}
    if body.stage not in valid_stages:
        raise HTTPException(status_code=400, detail=f"Invalid stage: {body.stage}")
    
    lead.stage = body.stage
    lead.updated_at = datetime.now(timezone.utc)
    await db.commit()
    
    return LeadStageResponse(
        lead_id=lead.lead_id,
        stage=lead.stage,
        score=lead.score,
        score_value=lead.score_value,
        updated_at=lead.updated_at,
    )


@router.get(
    "",
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def list_leads(
    db: DBSession,
    score: str | None = Query(default=None),
    stage: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Admin: list all leads with optional score/stage filters (A-08)."""
    log.info("lead.list", score=score, stage=stage, limit=limit, offset=offset)
    
    # Build query with filters
    q = select(Lead).order_by(Lead.qualified_at.desc())
    count_q = select(func.count()).select_from(Lead)
    
    if score:
        q = q.where(Lead.score == score)
        count_q = count_q.where(Lead.score == score)
    
    if stage:
        q = q.where(Lead.stage == stage)
        count_q = count_q.where(Lead.stage == stage)
    
    # Execute queries
    total = (await db.execute(count_q)).scalar() or 0
    result = await db.execute(
        q.offset(offset).limit(limit).options(selectinload(Lead.relances))
    )
    leads = result.scalars().all()
    
    # Format response
    items = []
    for lead in leads:
        customer = await db.get(Customer, lead.customer_id)
        items.append({
            "lead_id": str(lead.lead_id),
            "customer_id": lead.customer_id,
            "customer_name": customer.name if customer else None,
            "conversation_id": str(lead.conversation_id),
            "score": lead.score,
            "score_value": lead.score_value,
            "stage": lead.stage,
            "intent": lead.intent,
            "product_interest": lead.product_interest or [],
            "source": lead.source,
            "relance_count": lead.relance_count,
            "qualified_at": lead.qualified_at.isoformat(),
            "created_at": lead.created_at.isoformat(),
            "updated_at": lead.updated_at.isoformat(),
        })
    
    return {
        "leads": items,
        "total": total,
        "limit": limit,
        "offset": offset,
    }
