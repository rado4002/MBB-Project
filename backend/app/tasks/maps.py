"""
app/tasks/maps.py — Celery tasks for M7 MAPS Intelligence.

MAPS = Market-Aware Pattern Scoring.
Tags demand signals, silence reasons, and conversion triggers extracted
from conversations onto lead/customer records for analytics.
"""

from __future__ import annotations

import structlog
from celery import Task

from app.tasks.celery_app import celery_app

log = structlog.get_logger(__name__)


class _BaseTask(Task):
    abstract = True
    max_retries = 3
    default_retry_delay = 30


# ── Task: tag a conversation event ───────────────────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.maps.tag_event",
    queue="maps",
    acks_late=True,
)
def tag_event(
    self: Task,
    conversation_id: str,
    event_type: str,
    payload: dict,
) -> dict:
    """
    Extract MAPS signals from a conversation event and persist tags.

    Args:
        conversation_id: UUID of the Conversation row.
        event_type:      e.g. "silence", "opt_out", "purchase", "objection".
        payload:         Raw event context (message text, lead score, etc.).

    Returns dict with {"tagged": bool, "tags": [...]}.
    """
    import asyncio

    from app.modules.m8_maps import service as maps_svc  # type: ignore[import]

    log.info("maps.tag_event.start", conversation_id=conversation_id, event_type=event_type)
    try:
        result = asyncio.get_event_loop().run_until_complete(
            maps_svc.tag_event(
                conversation_id=conversation_id,
                event_type=event_type,
                payload=payload,
            )
        )
        log.info("maps.tag_event.done", conversation_id=conversation_id, tags=result.get("tags"))
        return result
    except Exception as exc:
        log.error("maps.tag_event.error", conversation_id=conversation_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 30)


# ── Periodic task: aggregate MAPS metrics (Beat-triggered) ───────────────────

@celery_app.task(
    name="app.tasks.maps.aggregate_daily",
    queue="maps",
    acks_late=True,
)
def aggregate_daily_maps() -> dict:
    """
    Periodic beat task (runs daily at 02:00 Kinshasa time).

    Computes daily demand patterns, silence rates, and conversion triggers.
    Results are stored in the maps_tags table and invalidate dashboard cache.
    """
    import asyncio

    from app.modules.m8_maps import service as maps_svc  # type: ignore[import]
    from app.cache import invalidate_prefix, make_key

    log.info("maps.aggregate_daily.start")
    result = asyncio.get_event_loop().run_until_complete(
        maps_svc.aggregate_daily()
    )
    # Invalidate analytics cache so dashboard picks up fresh data
    asyncio.get_event_loop().run_until_complete(
        invalidate_prefix(make_key("analytics", ""))
    )
    log.info("maps.aggregate_daily.done", result=result)
    return result
