"""
EP-15: POST /api/v1/maps/tags
EP-16: GET  /api/v1/maps/insights
A-10:  GET  /api/v1/maps/tags   (admin — raw tag list)
A-11:  PUT  /api/v1/maps/tags/{tag_id}/validate
"""
import uuid
from datetime import date, datetime, timezone

import structlog
from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import DBSession, IdempotencyKey, get_current_role, require_role
from app.models.maps_tag import MapsTag
from app.modules.m8_maps import tagger
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
    tag = MapsTag(
        message_id=uuid.uuid4(),  # Placeholder if no message context
        conversation_id=body.conversation_id,
        customer_id="api",
        category=body.category.value,
        pattern=body.tag,
        language=body.language.value if body.language else None,
        tag_metadata={"raw_signal": body.raw_signal, "confidence": body.confidence},
    )
    db.add(tag)
    await db.flush()
    return MapsTagResponse(
        tag_id=tag.tag_id,
        conversation_id=tag.conversation_id,
        category=body.category,
        tag=tag.pattern,
        language=body.language,
        confidence=body.confidence,
        created_at=tag.created_at or datetime.now(timezone.utc),
    )


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
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(period_end, datetime.max.time(), tzinfo=timezone.utc)
    result = await tagger.get_insights(db, start_dt, end_dt, category, limit)
    return MapsInsightsResponse(
        period_start=start_dt,
        period_end=end_dt,
        total_tags=result["total_tags"],
        insights=result["insights"],
    )


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
    tags, total = await tagger.list_tags(db, validated, category, limit, offset)
    return {"tags": tags, "total": total, "limit": limit, "offset": offset}


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
    try:
        result = await tagger.validate_tag(
            db, tag_id, body.validated, body.corrected_tag,
            body.corrected_category.value if body.corrected_category else None,
        )
        return MapsTagValidateResponse(
            tag_id=tag_id,
            validated=body.validated,
            updated_at=datetime.fromisoformat(result["updated_at"]),
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc))
