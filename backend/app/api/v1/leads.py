"""
EP-04: POST /api/v1/leads
EP-05: GET  /api/v1/leads/{lead_id}
EP-06: PUT  /api/v1/leads/{lead_id}/score
EP-07: PUT  /api/v1/leads/{lead_id}/stage
A-08:  GET  /api/v1/leads  (admin list)
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.schemas.leads import (
    LeadCreate,
    LeadCreatedResponse,
    LeadResponse,
    LeadScoreResponse,
    LeadScoreUpdate,
    LeadStageResponse,
    LeadStageUpdate,
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
    """Qualify a conversation into a lead (called internally after qualification flow)."""
    log.info("lead.create", conversation_id=str(body.conversation_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M5 not yet implemented")


@router.get(
    "/{lead_id}",
    response_model=LeadResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_lead(lead_id: uuid.UUID, db: DBSession):
    """Retrieve lead details with relance history."""
    log.info("lead.get", lead_id=str(lead_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M5 not yet implemented")


@router.put(
    "/{lead_id}/score",
    response_model=LeadScoreResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_lead_score(lead_id: uuid.UUID, body: LeadScoreUpdate, db: DBSession):
    """Update lead score as new signals arrive."""
    log.info("lead.score.update", lead_id=str(lead_id), score=body.score)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M5 not yet implemented")


@router.put(
    "/{lead_id}/stage",
    response_model=LeadStageResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_lead_stage(lead_id: uuid.UUID, body: LeadStageUpdate, db: DBSession):
    """Advance lead through awareness → consideration → decision funnel."""
    log.info("lead.stage.update", lead_id=str(lead_id), stage=body.stage)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M5 not yet implemented")


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
    log.info("lead.list", score=score, stage=stage)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A-08 not yet implemented")
