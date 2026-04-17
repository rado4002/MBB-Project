"""
app/modules/m9_dashboard/config.py — Feature flags + bot configuration.

Feature flags are stored in Redis for instant effect (no restart).
Bot configuration keys are stored in Redis with JSON values.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# Redis key prefixes
_FLAGS_KEY = "mbb:feature_flags"
_CONFIG_PREFIX = "mbb:config:"

# Default feature flags
DEFAULT_FLAGS = {
    "relance_enabled": True,
    "maps_enabled": True,
    "auto_escalation_enabled": True,
    "club_loyalty_enabled": True,
    "maintenance_mode": False,
}


async def get_feature_flags(redis) -> dict[str, Any]:
    """Get current feature flags from Redis. Returns defaults if not set."""
    try:
        raw = await redis.get(_FLAGS_KEY)
        if raw:
            flags = json.loads(raw)
            return {**DEFAULT_FLAGS, **flags, "updated_at": flags.get("updated_at", datetime.now(timezone.utc).isoformat())}
    except Exception:
        pass
    return {**DEFAULT_FLAGS, "updated_at": datetime.now(timezone.utc).isoformat()}


async def set_feature_flags(redis, flags: dict[str, Any]) -> dict[str, Any]:
    """Update feature flags in Redis. Immediately effective."""
    now = datetime.now(timezone.utc)
    data = {**flags, "updated_at": now.isoformat()}
    await redis.set(_FLAGS_KEY, json.dumps(data))
    log.info("config.flags.updated", flags=flags)
    return data


async def get_config(redis, key: str) -> dict[str, Any] | None:
    """Get a single config key."""
    try:
        raw = await redis.get(f"{_CONFIG_PREFIX}{key}")
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    return None


async def set_config(redis, key: str, value: Any, description: str | None = None) -> dict[str, Any]:
    """Set a config key. Returns the updated config entry."""
    now = datetime.now(timezone.utc)
    entry = {
        "key": key,
        "value": value,
        "description": description,
        "updated_at": now.isoformat(),
    }
    await redis.set(f"{_CONFIG_PREFIX}{key}", json.dumps(entry))
    log.info("config.key.updated", key=key)
    return entry


async def list_config(redis) -> list[dict[str, Any]]:
    """List all config keys."""
    keys = []
    try:
        async for key in redis.scan_iter(match=f"{_CONFIG_PREFIX}*"):
            raw = await redis.get(key)
            if raw:
                keys.append(json.loads(raw))
    except Exception:
        pass
    return keys


async def toggle_maintenance(redis, enabled: bool) -> dict[str, Any]:
    """Toggle maintenance mode via feature flags."""
    flags = await get_feature_flags(redis)
    flags["maintenance_mode"] = enabled
    return await set_feature_flags(redis, flags)
