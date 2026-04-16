from celery import Celery
from celery.schedules import crontab

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "mbb",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=[
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
        "app.tasks.relance.*":    {"queue": "relance"},
        "app.tasks.maps.*":       {"queue": "maps"},
        "app.tasks.escalation.*": {"queue": "escalation"},
        "app.tasks.conversion.*": {"queue": "conversion"},
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
        # M5 — Scan for due relances every 15 minutes
        "relance-process-due": {
            "task": "app.tasks.relance.process_due",
            "schedule": crontab(minute="*/15"),
            "options": {"queue": "relance"},
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
