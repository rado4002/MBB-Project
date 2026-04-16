"""
app/modules/m1_gateway/session_cache.py — Conversation session Redis HASH.

Stores live conversation state in Redis DB2 so workers don't hit PostgreSQL
on every message. Designed for the DRC hot-path: a single hgetall() per
inbound message instead of multiple DB queries.

Key format : session:{conversation_id}
Type       : HASH (Redis)
TTL        : 3 600 s (1 h) — refreshed on every message
DB         : 2 (application cache, decode_responses=True)

Fields
------
customer_id     str     phone_number (PK in mbb.customers)
language        str     lingala | french | swahili
current_topic   str     last identified topic (free text)
stage           str     qualifying | nurturing | escalated | converted
score           str     "0"–"10"  (stringified — Redis hashes are strings)
last_msg_time   str     ISO-8601 UTC
msg_count       str     stringified int
price_discussed str     "true" | "false"
products        str     JSON array of product slugs shown in this session
history         str     JSON array of {"direction","content","language"} dicts
                        capped at 20 entries (~10 exchanges)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

from app.cache import TTL, get_cache_client

log = structlog.get_logger(__name__)

_KEY_PREFIX = "session"
_MAX_HISTORY = 20   # cap — prevents unbounded growth


def _session_key(conversation_id: str) -> str:
    return f"{_KEY_PREFIX}:{conversation_id}"


# ── Typed session state ───────────────────────────────────────────────────────

@dataclass
class SessionState:
    """In-memory representation of a conversation session."""
    customer_id: str = ""
    language: str = "french"
    current_topic: str = ""
    stage: str = "qualifying"
    score: int = 0
    last_msg_time: str = ""
    msg_count: int = 0
    price_discussed: bool = False
    products: list[str] = field(default_factory=list)
    history: list[dict] = field(default_factory=list)

    @classmethod
    def from_hash(cls, h: dict[str, str]) -> "SessionState":
        """Deserialize a Redis HASH (all string values) into SessionState."""
        return cls(
            customer_id=h.get("customer_id", ""),
            language=h.get("language", "french"),
            current_topic=h.get("current_topic", ""),
            stage=h.get("stage", "qualifying"),
            score=int(h.get("score", "0") or "0"),
            last_msg_time=h.get("last_msg_time", ""),
            msg_count=int(h.get("msg_count", "0") or "0"),
            price_discussed=h.get("price_discussed", "false") == "true",
            products=json.loads(h.get("products", "[]") or "[]"),
            history=json.loads(h.get("history", "[]") or "[]"),
        )

    def to_hash(self) -> dict[str, str]:
        """Serialize to Redis HASH mapping (all string values)."""
        return {
            "customer_id": self.customer_id,
            "language": self.language,
            "current_topic": self.current_topic,
            "stage": self.stage,
            "score": str(self.score),
            "last_msg_time": self.last_msg_time,
            "msg_count": str(self.msg_count),
            "price_discussed": "true" if self.price_discussed else "false",
            "products": json.dumps(self.products),
            "history": json.dumps(self.history[-_MAX_HISTORY:]),
        }


# ── Public operations ─────────────────────────────────────────────────────────

async def get_session(conversation_id: str) -> SessionState | None:
    """
    Load session from Redis. Returns None on cache miss or error.
    decode_responses=True — all hash values are already ``str``.
    """
    try:
        client = get_cache_client()
        raw: dict[str, str] = await client.hgetall(_session_key(conversation_id))
        await client.aclose()
        if not raw:
            return None
        return SessionState.from_hash(raw)
    except Exception as exc:
        log.warning("session_cache.get_failed", conv_id=conversation_id, error=str(exc))
        return None


async def save_session(conversation_id: str, state: SessionState) -> bool:
    """
    Persist session to Redis and refresh the 1-hour TTL.
    Returns True on success (failures are swallowed — DB is source of truth).
    """
    try:
        client = get_cache_client()
        key = _session_key(conversation_id)
        await client.hset(key, mapping=state.to_hash())
        await client.expire(key, TTL.SESSION)
        await client.aclose()
        log.debug("session_cache.saved", conv_id=conversation_id)
        return True
    except Exception as exc:
        log.warning("session_cache.save_failed", conv_id=conversation_id, error=str(exc))
        return False


async def append_exchange(
    conversation_id: str,
    *,
    inbound_content: str,
    outbound_content: str,
    language: str,
) -> bool:
    """
    Append one inbound + one outbound message to the session history list
    and refresh the TTL — without reloading the full session from Redis.

    Uses HGET + HSET on just the `history` field to minimise round-trips.
    """
    try:
        client = get_cache_client()
        key = _session_key(conversation_id)
        raw_history = await client.hget(key, "history") or "[]"
        history: list[dict] = json.loads(raw_history)
        history.append({"direction": "inbound", "content": inbound_content, "language": language})
        history.append({"direction": "outbound", "content": outbound_content, "language": language})
        history = history[-_MAX_HISTORY:]
        await client.hset(key, mapping={
            "history": json.dumps(history),
            "last_msg_time": datetime.now(timezone.utc).isoformat(),
            "msg_count": str(int(await client.hget(key, "msg_count") or "0") + 1),
        })
        await client.expire(key, TTL.SESSION)
        await client.aclose()
        return True
    except Exception as exc:
        log.warning("session_cache.append_failed", conv_id=conversation_id, error=str(exc))
        return False


async def invalidate_session(conversation_id: str) -> None:
    """Delete session from cache (e.g. on conversion or escalation)."""
    try:
        client = get_cache_client()
        await client.delete(_session_key(conversation_id))
        await client.aclose()
    except Exception as exc:
        log.warning("session_cache.invalidate_failed", conv_id=conversation_id, error=str(exc))
