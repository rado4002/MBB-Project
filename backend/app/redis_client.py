"""
app/redis_client.py — Shared Redis connection pool + Blackout Queue.

Redis DBs:
  DB 0 — Celery broker  (managed by Celery)
  DB 1 — Celery results (managed by Celery)
  DB 2 — Application cache (app/cache.py)
  DB 3 — Blackout queue (this module)  ← AOF-persisted

Blackout Queue
--------------
Messages received during power outages are pushed onto a Redis List
(DB 3, key "mbb:blackout:queue"). When power/connectivity returns, the
worker drains the queue in FIFO order and processes each message normally.

All AOF persistence for DB 3 is configured in redis.conf / Docker Compose.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
import structlog
from prometheus_client import Gauge

from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

# Prometheus gauge — tracks live depth of the blackout recovery queue.
# Updated on every enqueue, dequeue, and queue-length check so Prometheus
# scrapes always reflect the current state without an extra Redis call.
BLACKOUT_QUEUE_DEPTH: Gauge = Gauge(
    "mbb_blackout_queue_depth",
    "Number of messages waiting in the Redis blackout recovery queue (DB 3)",
)

# ── Broker pool (DB 0 — used by FastAPI endpoints for pub/sub checks) ─────────
_pool: aioredis.ConnectionPool | None = None


def get_redis_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            max_connections=20,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
    return _pool


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """FastAPI dependency — yields a Redis client from the shared pool (DB 0)."""
    client = aioredis.Redis(connection_pool=get_redis_pool())
    try:
        yield client
    finally:
        await client.aclose()


# ── Blackout queue (DB 3 — AOF-persisted) ────────────────────────────────────
_BLACKOUT_DB = 3
_BLACKOUT_KEY = "mbb:blackout:queue"
_BLACKOUT_PROCESSING_KEY = "mbb:blackout:processing"
_BLACKOUT_QUARANTINE_KEY = "mbb:blackout:quarantine"
_BLACKOUT_DRAIN_LOCK_KEY = "mbb:blackout:drain:lock"
_BLACKOUT_DRAIN_LOCK_TTL_SECONDS = 300
_blackout_pool: aioredis.ConnectionPool | None = None

_RELEASE_LOCK_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
end
return 0
"""


def _get_blackout_pool() -> aioredis.ConnectionPool:
    global _blackout_pool
    if _blackout_pool is None:
        url = f"redis://{settings.redis_host}:{settings.redis_port}/{_BLACKOUT_DB}"
        _blackout_pool = aioredis.ConnectionPool.from_url(
            url,
            max_connections=5,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
    return _blackout_pool


def _blackout_client() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_get_blackout_pool())


async def blackout_enqueue(message: dict[str, Any]) -> bool:
    """
    Push *message* (JSON-serializable dict) onto the tail of the blackout queue.

    Called by M1 Gateway when normal processing fails due to a downstream
    outage.  Returns True on success.
    """
    try:
        client = _blackout_client()
        await client.rpush(_BLACKOUT_KEY, json.dumps(message, default=str))
        await client.aclose()
        BLACKOUT_QUEUE_DEPTH.inc()
        log.info("blackout.enqueued")
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("blackout.enqueue_failed", error=str(exc))
        return False


async def blackout_dequeue_batch(batch_size: int = 50) -> list[dict[str, Any]]:
    """Disabled compatibility shim for the former destructive dequeue API."""
    del batch_size
    raise RuntimeError("destructive blackout dequeue is disabled; use claim/ack")


def new_blackout_drain_owner() -> str:
    """Return an opaque unique owner token for one drain invocation."""
    return uuid.uuid4().hex


async def blackout_acquire_drain_lock(
    owner: str,
    *,
    ttl_seconds: int = _BLACKOUT_DRAIN_LOCK_TTL_SECONDS,
) -> bool:
    """Acquire the bounded canonical-drainer lock for *owner*."""
    client = _blackout_client()
    try:
        result = await client.set(
            _BLACKOUT_DRAIN_LOCK_KEY,
            owner,
            nx=True,
            ex=ttl_seconds,
        )
        return bool(result)
    finally:
        await client.aclose()


async def blackout_release_drain_lock(owner: str) -> bool:
    """Release the drain lock only when it is still owned by *owner*."""
    client = _blackout_client()
    try:
        result = await client.eval(
            _RELEASE_LOCK_SCRIPT,
            1,
            _BLACKOUT_DRAIN_LOCK_KEY,
            owner,
        )
        return bool(result)
    finally:
        await client.aclose()


async def blackout_claim_one() -> str | None:
    """Atomically move the oldest pending serialized item into processing."""
    client = _blackout_client()
    try:
        return await client.lmove(
            _BLACKOUT_KEY,
            _BLACKOUT_PROCESSING_KEY,
            "LEFT",
            "RIGHT",
        )
    finally:
        await client.aclose()


async def blackout_acknowledge(raw: str) -> bool:
    """Remove one exact serialized item from processing after publication."""
    client = _blackout_client()
    try:
        removed = await client.lrem(_BLACKOUT_PROCESSING_KEY, 1, raw)
        return int(removed) == 1
    finally:
        await client.aclose()


async def blackout_quarantine(raw: str) -> bool:
    """Copy malformed *raw* to quarantine, then remove its processing claim."""
    client = _blackout_client()
    try:
        await client.rpush(_BLACKOUT_QUARANTINE_KEY, raw)
        removed = await client.lrem(_BLACKOUT_PROCESSING_KEY, 1, raw)
        return int(removed) == 1
    finally:
        await client.aclose()


async def blackout_recover_processing() -> int:
    """Restore abandoned claims ahead of newer pending items in FIFO order."""
    client = _blackout_client()
    recovered = 0
    try:
        while True:
            raw = await client.lmove(
                _BLACKOUT_PROCESSING_KEY,
                _BLACKOUT_KEY,
                "RIGHT",
                "LEFT",
            )
            if raw is None:
                return recovered
            recovered += 1
    finally:
        await client.aclose()


async def blackout_depths() -> dict[str, int]:
    """Return authoritative pending, processing, and quarantine list depths."""
    client = _blackout_client()
    try:
        pipeline = client.pipeline()
        pipeline.llen(_BLACKOUT_KEY)
        pipeline.llen(_BLACKOUT_PROCESSING_KEY)
        pipeline.llen(_BLACKOUT_QUARANTINE_KEY)
        pending, processing, quarantine = await pipeline.execute()
        BLACKOUT_QUEUE_DEPTH.set(int(pending))
        return {
            "pending_depth": int(pending),
            "processing_depth": int(processing),
            "quarantine_depth": int(quarantine),
        }
    finally:
        await client.aclose()


async def blackout_queue_length() -> int:
    """Return the current number of queued messages (for health checks)."""
    try:
        client = _blackout_client()
        length = await client.llen(_BLACKOUT_KEY)
        await client.aclose()
        val = int(length)
        BLACKOUT_QUEUE_DEPTH.set(val)  # authoritative sync from Redis
        return val
    except Exception:  # noqa: BLE001
        return -1
