"""
No-AI adapter for intentional local fallback mode.

This adapter never creates an external API client and never performs network I/O.
M1 catches the generated error and persists the existing localized fallback
response.
"""
from __future__ import annotations

import structlog

from app.adapters.base import BaseAIAdapter

log = structlog.get_logger(__name__)


class AIAdapterDisabled(RuntimeError):
    """Raised when AI generation is intentionally disabled."""


class DisabledAIAdapter(BaseAIAdapter):
    """Explicit no-AI adapter used by AI_ADAPTER=disabled or AI_ADAPTER=local."""

    async def generate(self, prompt: str, system: str, max_tokens: int) -> str:
        log.info("ai.disabled.local_fallback", chars=len(prompt), max_tokens=max_tokens)
        raise AIAdapterDisabled("AI adapter disabled; using local fallback response")

    async def detect_language(self, text: str) -> str:
        log.info("ai.disabled.language_default", chars=len(text))
        return "french"
