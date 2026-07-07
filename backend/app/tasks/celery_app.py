import asyncio

from celery import Celery
from celery.schedules import crontab
from celery.signals import worker_process_init

from app.config import get_settings

settings = get_settings()
_worker_loop: asyncio.AbstractEventLoop | None = None


@worker_process_init.connect
def reset_db_pool(**kwargs):
    """Dispose SQLAlchemy async engine pool after Celery worker fork.

    Without this, forked workers inherit the parent's connection pool whose
    internal asyncpg Futures are bound to the parent's event loop — causing
    'Future attached to a different loop' errors on every task.
    """
    global _worker_loop
    _worker_loop = None
    from app.database import engine
    engine.sync_engine.dispose(close=False)


def run_async(coro):
    """Run async Celery work on one event loop per worker process."""
    global _worker_loop
    if _worker_loop is None or _worker_loop.is_closed():
        _worker_loop = asyncio.new_event_loop()
    asyncio.set_event_loop(_worker_loop)
    return _worker_loop.run_until_complete(coro)


celery_app = Celery(
    "mbb",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
        "app.tasks.m1",
        "app.tasks.m5",
        "app.tasks.qualification",
        "app.tasks.relance",
        "app.tasks.maps",
        "app.tasks.escalation",
        "app.tasks.conversion",
    ],
)

celery_app.conf.update(
    # Serialization
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Timezone (Kinshasa = UTC+2)
    timezone=settings.celery_timezone,
    enable_utc=True,

    # Reliability — critical for DRC power outages
    task_acks_late=True,                # Acknowledge only after task completes
    task_reject_on_worker_lost=True,    # Re-queue if worker dies
    task_track_started=True,
    worker_prefetch_multiplier=1,       # One task at a time per worker slot

    # Result expiry
    result_expires=86400,               # 24 hours

    # Named queues (one per business domain)
    task_routes={
        "m1.*":                          {"queue": "default"},
        "app.tasks.m1.*":                {"queue": "default"},
        "app.tasks.m5.*":                {"queue": "default"},
        "app.tasks.qualification.*":     {"queue": "default"},
        "app.tasks.relance.*":           {"queue": "relance"},
        "app.tasks.maps.*":              {"queue": "maps"},
        "app.tasks.escalation.*":        {"queue": "escalation"},
        "app.tasks.conversion.*":        {"queue": "conversion"},
    },
    task_default_queue="default",

    # RedBeat (Redis-backed Beat scheduler — survives restarts)
    redbeat_redis_url=settings.redis_url,
    redbeat_key_prefix="mbb:redbeat:",
    redbeat_lock_timeout=5 * 60,        # 5 min lock to prevent double-firing

    # Retry defaults (individual tasks override as needed)
    task_max_retries=3,

    # ── Beat schedule ─────────────────────────────────────────────────────────
    # All times are in Africa/Kinshasa (UTC+2).
    # No relance tasks are dispatched between 22:00–07:00 (enforced in M6 service).
    beat_schedule={
        # M6 — Scan for eligible leads every hour (Sprint 1.6)
        "relance-scan-eligible": {
            "task": "app.tasks.relance.scan_eligible_leads",
            "schedule": crontab(minute=0),  # Every hour at :00
            "options": {"queue": "relance"},
        },

        # M1 — Drain blackout queue every 5 minutes (power recovery)
        "m1-drain-blackout": {
            "task": "m1.drain_blackout_queue",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "default"},
        },

        # M6 — Drain blackout queue every 5 minutes
        "conversion-drain-blackout": {
            "task": "app.tasks.conversion.drain_blackout_queue",
            "schedule": crontab(minute="*/5"),
            "options": {"queue": "default"},
        },

        # M7 — Daily MAPS aggregation at 02:00 Kinshasa (00:00 UTC)
        "maps-aggregate-daily": {
            "task": "app.tasks.maps.aggregate_daily",
            "schedule": crontab(hour=0, minute=0),
            "options": {"queue": "maps"},
        },

        # M8 — Check for stale escalations every 30 minutes
        "escalation-check-stale": {
            "task": "app.tasks.escalation.check_stale",
            "schedule": crontab(minute="*/30"),
            "options": {"queue": "escalation"},
        },
    },
)
