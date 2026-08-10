"""Compatibility access to the MBB-owned AI system policy."""
from __future__ import annotations

def get_system_prompt(language: str, history: list[dict] | None = None) -> str:
    """Return policy text without interpolating legacy runtime history."""
    from app.ai.policy import get_system_policy

    del history
    return get_system_policy(language).text
