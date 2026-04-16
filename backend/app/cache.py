"""
app/cache.py — Redis cache utility for MBB ya Kin.

Provides:
  - Namespaced key builder (mbb:<namespace>:<key>)
  - get / set / delete with automatic JSON serialization
  - invalidate_prefix — bulk-delete by key pattern
  - cache_or_compute — async cache-aside helper

All operations are best-effort: a Redis failure never crashes the caller
(logged and silently bypassed so the system degrades gracefully during
DRC network instability).

Redis DBs:
  DB 0 — Celery broker queue   (managed by Celery)
  DB 1 — Celery result backend (managed by Celery)
  DB 2 — Application cache     ← used here
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import redis.asyncio as aioredis
import structlog

from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

# ── Cache DB (separate from broker/backend) ───────────────────────────────────
_CACHE_DB = 2
_cache_pool: aioredis.ConnectionPool | None = None


def _get_cache_pool() -> aioredis.ConnectionPool:
    global _cache_pool
    if _cache_pool is None:
        cache_url = (
            f"redis://{settings.redis_host}:{settings.redis_port}/{_CACHE_DB}"
        )
        _cache_pool = aioredis.ConnectionPool.from_url(
            cache_url,
            max_connections=10,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=3,
            retry_on_timeout=True,
        )
    return _cache_pool


def _cache_client() -> aioredis.Redis:
    return aioredis.Redis(connection_pool=_get_cache_pool())


# ── Key builder ───────────────────────────────────────────────────────────────

def make_key(namespace: str, *parts: str | int) -> str:
    """Build a namespaced cache key: mbb:<namespace>:<part1>:<part2>..."""
    segments = ":".join(str(p) for p in parts)
    return f"mbb:{namespace}:{segments}"


# ── Core operations ───────────────────────────────────────────────────────────

async def cache_get(key: str) -> Any | None:
    """Return the cached value (deserialized) or None on miss / error."""
    try:
        client = _cache_client()
        raw = await client.get(key)
        await client.aclose()
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache.get_failed", key=key, error=str(exc))
        return None


async def cache_set(key: str, value: Any, ttl_seconds: int = 300) -> bool:
    """Serialize and store *value* under *key* with a TTL. Returns True on success."""
    try:
        client = _cache_client()
        await client.setex(key, ttl_seconds, json.dumps(value, default=str))
        await client.aclose()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("cache.set_failed", key=key, error=str(exc))
        return False


async def cache_delete(key: str) -> bool:
    """Delete a single key. Returns True on success."""
    try:
        client = _cache_client()
        await client.delete(key)
        await client.aclose()
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("cache.delete_failed", key=key, error=str(exc))
        return False


async def invalidate_prefix(prefix: str) -> int:
    """
    Delete all keys matching *prefix**.

    Uses SCAN to avoid blocking Redis on large key spaces.
    Returns the number of deleted keys.
    """
    deleted = 0
    try:
        client = _cache_client()
        async for key in client.scan_iter(match=f"{prefix}*", count=100):
            await client.delete(key)
            deleted += 1
        await client.aclose()
        log.info("cache.invalidate_prefix", prefix=prefix, deleted=deleted)
    except Exception as exc:  # noqa: BLE001
        log.warning("cache.invalidate_prefix_failed", prefix=prefix, error=str(exc))
    return deleted


# ── Cache-aside helper ────────────────────────────────────────────────────────

_T = TypeVar("_T")


async def cache_or_compute(
    key: str,
    compute: Callable[[], Awaitable[_T]],
    ttl_seconds: int = 300,
) -> _T:
    """
    Return the cached value for *key*. On miss, call *compute()*, cache the
    result, and return it.

    Usage::

        data = await cache_or_compute(
            make_key("lead", lead_id),
            lambda: db.get(Lead, lead_id),
            ttl_seconds=120,
        )
    """
    cached = await cache_get(key)
    if cached is not None:
        log.debug("cache.hit", key=key)
        return cached  # type: ignore[return-value]

    log.debug("cache.miss", key=key)
    result = await compute()
    await cache_set(key, result, ttl_seconds)
    return result


# ── Named TTLs (seconds) ──────────────────────────────────────────────────────
class TTL:
    """Canonical TTL constants — keep expiry logic out of business code."""

    SESSION = 3_600        # 1 hour  — conversation context
    LEAD = 300             # 5 min   — lead score / profile
    CATALOG = 1_800        # 30 min  — product catalog (static in Phase 1)
    ANALYTICS = 600        # 10 min  — funnel / dashboard aggregates
    RATE_LIMIT = 60        # 1 min   — request rate limit window
    LOCK = 30              # 30 sec  — idempotency / distributed lock
    DEDUP = 86_400         # 24 h    — message deduplication
    CUSTOMER = 600         # 10 min  — customer profile (language, opt-out)


# ── Public client accessor ─────────────────────────────────────────────────────


def get_cache_client() -> aioredis.Redis:
    """
    Return a Redis client connected to DB2 (application cache).

    The pool uses ``decode_responses=True`` — all keys and values are
    returned as ``str``, not ``bytes``. Callers must NOT call ``.decode()``
    on responses.

    The caller is responsible for closing the client after use when it is
    opened outside a request context (Celery tasks).  FastAPI routes should
    use the ``RedisClient`` dependency from ``app.api.deps`` instead.
    """
    return _cache_client()
