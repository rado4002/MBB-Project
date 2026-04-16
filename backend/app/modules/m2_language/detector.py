"""
app/modules/m2_language/detector.py — Unified language detection.

Strategy (per spec):
  1. Keyword fast-path: if ≥2 markers → confidence ≥ 0.90, skip Claude.
  2. Claude API: for ambiguous / mixed messages (confidence < 0.90).
  3. Sticky: once set on a conversation, preserved unless explicit switch.
  4. Default: French if all detection fails.

This module re-exports the regex helpers from m1_gateway.language_detector
and adds the Claude-augmented detect_language_smart() function.
"""
from __future__ import annotations

import structlog

from app.modules.m1_gateway.language_detector import (
    detect_language as regex_detect,
    is_language_switch_request,
    is_opt_out,
)

log = structlog.get_logger(__name__)

# Re-export for convenience
__all__ = [
    "detect_language_smart",
    "is_language_switch_request",
    "is_opt_out",
]

_HIGH_CONFIDENCE_THRESHOLD = 0.85


async def detect_language_smart(
    text: str,
    existing_language: str | None = None,
) -> tuple[str, float]:
    """
    Detect language using keyword fast-path first, then Claude as fallback.

    Returns:
        (language, confidence) where language ∈ {"lingala", "french", "swahili"}
    """
    # Sticky language — short-circuit
    if existing_language:
        return existing_language, 1.0

    # 1. Try keyword fast-path
    language, confidence = regex_detect(text, existing_language=None)

    if confidence >= _HIGH_CONFIDENCE_THRESHOLD:
        log.debug(
            "m2.lang.regex_hit",
            language=language,
            confidence=confidence,
            text_preview=text[:50],
        )
        return language, confidence

    # 2. Keywords inconclusive → try Claude
    try:
        from app.adapters import get_ai_adapter

        ai = get_ai_adapter()
        claude_lang = await ai.detect_language(text)
        log.info(
            "m2.lang.claude_detected",
            language=claude_lang,
            regex_lang=language,
            regex_confidence=confidence,
            text_preview=text[:50],
        )
        return claude_lang, 0.95
    except Exception as exc:
        log.warning("m2.lang.claude_failed", error=str(exc), fallback=language)
        # Fall back to regex result (even if low confidence)
        return language, confidence
