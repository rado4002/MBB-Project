"""
EP-15: POST /api/v1/maps/tags
EP-16: GET  /api/v1/maps/insights
A-10:  GET  /api/v1/maps/tags   (admin — raw tag list)
A-11:  PUT  /api/v1/maps/tags/{tag_id}/validate
"""
import uuid
from datetime import date

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.schemas.maps import (
    MapsInsightsResponse,
    MapsTagCreate,
    MapsTagResponse,
    MapsTagValidate,
    MapsTagValidateResponse,
)

log = structlog.get_logger()
router = APIRouter(prefix="/maps", tags=["M8 — MAPS Intelligence"])


@router.post(
    "/tags",
    response_model=MapsTagResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_role)],
)
async def create_maps_tag(
    body: MapsTagCreate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Tag a demand signal, silence reason, or conversion trigger from a conversation."""
    log.info("maps.tag.create", category=body.category, tag=body.tag)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M8 not yet implemented")


@router.get(
    "/insights",
    response_model=MapsInsightsResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_maps_insights(
    db: DBSession,
    period_start: date = Query(...),
    period_end: date = Query(...),
    category: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
):
    """Aggregated MAPS intelligence: top tags, trends, by category and period."""
    log.info("maps.insights.get", period_start=str(period_start), period_end=str(period_end))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M8 not yet implemented")


@router.get(
    "/tags",
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def list_maps_tags(
    db: DBSession,
    validated: bool | None = Query(default=None),
    category: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    """Admin: list raw MAPS tags with optional filters (A-10)."""
    log.info("maps.tags.list", category=category)
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A-10 not yet implemented")


@router.put(
    "/tags/{tag_id}/validate",
    response_model=MapsTagValidateResponse,
    dependencies=[Depends(require_role("admin", "orchestrator"))],
)
async def validate_maps_tag(
    tag_id: uuid.UUID,
    body: MapsTagValidate,
    db: DBSession,
):
    """Admin: validate or correct an AI-generated MAPS tag (A-11)."""
    log.info("maps.tag.validate", tag_id=str(tag_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="A-11 not yet implemented")
