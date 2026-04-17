"""
app/modules/m8_maps/service.py — M8 service facade.

Called by Celery tasks. Opens its own DB session.
"""
from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.database import AsyncSessionLocal

log = structlog.get_logger(__name__)


async def tag_event(
    conversation_id: str,
    event_type: str,
    payload: dict,
) -> dict[str, Any]:
    """
    Tag a conversation event with MAPS signals.
    Called by Celery task app.tasks.maps.tag_event.
    """
    from app.modules.m8_maps.tagger import tag_message

    async with AsyncSessionLocal() as session:
        message_id = uuid.UUID(payload.get("message_id", str(uuid.uuid4())))
        customer_id = payload.get("customer_id", "")
        content = payload.get("content", "")
        content_type = payload.get("content_type", "text")
        language = payload.get("language", "french")
        city = payload.get("city")

        tags = await tag_message(
            session=session,
            message_id=message_id,
            conversation_id=uuid.UUID(conversation_id),
            customer_id=customer_id,
            content=content,
            content_type=content_type,
            language=language,
            city=city,
        )
        await session.commit()
        return {"tagged": True, "tags": tags}


async def aggregate_daily() -> dict[str, Any]:
    """
    Refresh materialized view for dashboard.
    Called by Celery Beat task app.tasks.maps.aggregate_daily.
    """
    from sqlalchemy import text

    async with AsyncSessionLocal() as session:
        try:
            await session.execute(
                text("REFRESH MATERIALIZED VIEW CONCURRENTLY mbb.mv_daily_maps_summary")
            )
            await session.commit()
            log.info("maps.aggregate_daily.refreshed")
            return {"refreshed": True}
        except Exception as exc:
            # View may not exist yet in dev
            log.warning("maps.aggregate_daily.skip", error=str(exc))
            await session.rollback()
            return {"refreshed": False, "reason": str(exc)}
