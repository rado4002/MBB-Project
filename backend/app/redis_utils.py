"""
app/redis_utils.py — Reusable Redis helpers for M1–M9.

All helpers are best-effort: Redis failures never crash the caller.
Every module that needs rate-limiting, dedup, or caching imports from here.

Sections
--------
1. Rate limiter  — sliding counter per phone/IP
2. Message dedup — idempotency by whatsapp_message_id (24 h TTL)
3. Customer cache — hot reads for language + opt-out flag (DB2, 10 min)
4. Lead cache     — lead score + stage for M3/M4 (DB2, 5 min)
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

import structlog

from app.cache import TTL, get_cache_client, make_key

log = structlog.get_logger(__name__)


def _safe_identifier(value: str) -> str:
    """Return a stable, non-reversible reference suitable for logs."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


# ═══════════════════════════════════════════════════════════════════════════════
# 1. Rate limiter
# ═══════════════════════════════════════════════════════════════════════════════

async def rate_limit_check(
    phone: str,
    *,
    limit: int = 10,
    window_seconds: int = TTL.RATE_LIMIT,
) -> bool:
    """
    Increment the per-phone request counter (DB2).

    Returns True  → caller IS rate-limited (reject the request).
    Returns False → within limit (allow).

    The key expires automatically after *window_seconds*.
    """
    key = make_key("ratelimit", phone)
    try:
        client = get_cache_client()
        count = await client.incr(key)
        if count == 1:
            await client.expire(key, window_seconds)
        await client.aclose()
        if count > limit:
            log.warning(
                "rate_limit.exceeded",
                phone_ref=_safe_identifier(phone),
                count=count,
                limit=limit,
            )
            return True
        return False
    except Exception as exc:
        log.warning(
            "rate_limit.redis_error",
            phone_ref=_safe_identifier(phone),
            error_type=type(exc).__name__,
        )
        return False   # fail-open during Redis outage


async def rate_limit_remaining(phone: str, limit: int = 10) -> int:
    """Return how many requests remain in the current window (best-effort)."""
    key = make_key("ratelimit", phone)
    try:
        client = get_cache_client()
        raw = await client.get(key)
        await client.aclose()
        used = int(raw or "0")
        return max(0, limit - used)
    except Exception:
        return limit   # fail-safe


# ═══════════════════════════════════════════════════════════════════════════════
# 2. Message deduplication
# ═══════════════════════════════════════════════════════════════════════════════

async def has_accepted_inbound(whatsapp_message_id: str) -> bool:
    """Return whether this authoritative ID has a previously accepted marker."""
    key = make_key("dedup", whatsapp_message_id)
    try:
        client = get_cache_client()
        result = await client.exists(key)
        await client.aclose()
        return bool(result)
    except Exception as exc:
        log.warning(
            "dedup.accepted_marker_read_failed",
            wa_ref=_safe_identifier(whatsapp_message_id),
            error_type=type(exc).__name__,
        )
        return False


async def mark_inbound_accepted(whatsapp_message_id: str) -> bool:
    """Best-effort mark an inbound ID after a processing path accepts it."""
    key = make_key("dedup", whatsapp_message_id)
    try:
        client = get_cache_client()
        result = await client.set(key, "1", ex=TTL.DEDUP)
        await client.aclose()
        if result:
            return True
        log.warning(
            "dedup.accepted_marker_write_failed",
            wa_ref=_safe_identifier(whatsapp_message_id),
            error_type="RedisWriteNotConfirmed",
        )
        return False
    except Exception as exc:
        log.warning(
            "dedup.accepted_marker_write_failed",
            wa_ref=_safe_identifier(whatsapp_message_id),
            error_type=type(exc).__name__,
        )
        return False

async def dedup_check_and_mark(wa_message_id: str) -> bool:
    """
    Check if *wa_message_id* was already processed, then mark it.

    Returns True  → duplicate (caller should skip processing).
    Returns False → first time seen (safe to process).

    Uses a SET NX (set-if-not-exists) so check + mark is atomic.
    TTL: 24 hours (TTL.DEDUP).
    """
    key = make_key("dedup", wa_message_id)
    try:
        client = get_cache_client()
        # SET key "1" NX EX ttl — returns True if set (new), None if already existed
        result = await client.set(key, "1", nx=True, ex=TTL.DEDUP)
        await client.aclose()
        is_duplicate = result is None   # None → key already existed
        if is_duplicate:
            log.info("dedup.duplicate_detected", wa_ref=_safe_identifier(wa_message_id))
        return is_duplicate
    except Exception as exc:
        log.warning(
            "dedup.redis_error",
            wa_ref=_safe_identifier(wa_message_id),
            error_type=type(exc).__name__,
        )
        return False   # fail-open: process even if we can't check dedup


# ═══════════════════════════════════════════════════════════════════════════════
# 3. Customer profile cache
# ═══════════════════════════════════════════════════════════════════════════════

def _customer_key(phone: str) -> str:
    return make_key("customer", phone)


async def cache_customer_profile(phone: str, profile: dict[str, Any]) -> None:
    """
    Store a lightweight customer profile dict in DB2 (10-min TTL).

    Expected fields: language, opt_out_flag, city, club_member, club_points.
    Only cache what's needed for hot-path decisions — not the full ORM model.
    """
    key = _customer_key(phone)
    try:
        client = get_cache_client()
        await client.setex(key, TTL.CUSTOMER, json.dumps(profile, default=str))
        await client.aclose()
    except Exception as exc:
        log.warning("customer_cache.set_failed", phone=phone, error=str(exc))


async def get_cached_customer_profile(phone: str) -> dict[str, Any] | None:
    """Return cached customer profile or None on miss."""
    key = _customer_key(phone)
    try:
        client = get_cache_client()
        raw = await client.get(key)
        await client.aclose()
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        log.warning("customer_cache.get_failed", phone=phone, error=str(exc))
        return None


async def invalidate_customer_cache(phone: str) -> None:
    """Remove the customer profile cache entry (call on opt-out or data change)."""
    key = _customer_key(phone)
    try:
        client = get_cache_client()
        await client.delete(key)
        await client.aclose()
    except Exception as exc:
        log.warning("customer_cache.invalidate_failed", phone=phone, error=str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# 4. Lead score / stage cache
# ═══════════════════════════════════════════════════════════════════════════════

def _lead_key(lead_id: str) -> str:
    return make_key("lead", lead_id)


async def cache_lead(lead_id: str, lead_data: dict[str, Any]) -> None:
    """
    Store a lead summary dict in DB2 (5-min TTL).

    Expected fields: score_numeric, score_label (hot/warm/cold),
    stage, relance_count, last_interaction.
    """
    key = _lead_key(lead_id)
    try:
        client = get_cache_client()
        await client.setex(key, TTL.LEAD, json.dumps(lead_data, default=str))
        await client.aclose()
    except Exception as exc:
        log.warning("lead_cache.set_failed", lead_id=lead_id, error=str(exc))


async def get_cached_lead(lead_id: str) -> dict[str, Any] | None:
    """Return cached lead summary or None on miss."""
    key = _lead_key(lead_id)
    try:
        client = get_cache_client()
        raw = await client.get(key)
        await client.aclose()
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:
        log.warning("lead_cache.get_failed", lead_id=lead_id, error=str(exc))
        return None


async def invalidate_lead_cache(lead_id: str) -> None:
    """Remove lead cache entry (call after score update or conversion)."""
    key = _lead_key(lead_id)
    try:
        client = get_cache_client()
        await client.delete(key)
        await client.aclose()
    except Exception as exc:
        log.warning("lead_cache.invalidate_failed", lead_id=lead_id, error=str(exc))
