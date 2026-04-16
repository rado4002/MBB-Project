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
from typing import Any, AsyncGenerator

import redis.asyncio as aioredis
import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

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
_blackout_pool: aioredis.ConnectionPool | None = None


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
        log.info("blackout.enqueued", wa_id=message.get("wa_id"))
        return True
    except Exception as exc:  # noqa: BLE001
        log.error("blackout.enqueue_failed", error=str(exc))
        return False


async def blackout_dequeue_batch(batch_size: int = 50) -> list[dict[str, Any]]:
    """
    Pop up to *batch_size* messages from the front (FIFO) of the blackout queue.

    Called by the Celery recovery task on system startup / connectivity restore.
    """
    messages: list[dict[str, Any]] = []
    try:
        client = _blackout_client()
        pipeline = client.pipeline()
        for _ in range(batch_size):
            pipeline.lpop(_BLACKOUT_KEY)
        results = await pipeline.execute()
        await client.aclose()
        for raw in results:
            if raw is None:
                break
            try:
                messages.append(json.loads(raw))
            except json.JSONDecodeError:
                log.warning("blackout.invalid_payload", raw=raw)
    except Exception as exc:  # noqa: BLE001
        log.error("blackout.dequeue_failed", error=str(exc))
    return messages


async def blackout_queue_length() -> int:
    """Return the current number of queued messages (for health checks)."""
    try:
        client = _blackout_client()
        length = await client.llen(_BLACKOUT_KEY)
        await client.aclose()
        return int(length)
    except Exception:  # noqa: BLE001
        return -1
