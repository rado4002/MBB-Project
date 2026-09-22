"""
app/modules/m6_relance/hooks.py — Deterministic value hooks for relances.

Selects localized fallback messages without invoking an AI provider.
Each relance uses a different persuasion angle (3 attempts maximum).

Hook Types (per Phase 1.B spec):
  1. Relance #1 (+24h): Value reminder — "Hey! The product you looked at is still available"
  2. Relance #2 (+48-72h): Social proof — "Others in [city] are loving this..."
  3. Relance #3 (+7-10d): Exclusive offer — "Last chance — special offer just for you"
"""
from __future__ import annotations

import structlog

from app.i18n import messages as i18n
from app.schemas.common import Language

log = structlog.get_logger(__name__)


async def generate_relance_hook(
    *,
    attempt_number: int,
    language: Language,
    product_interest: str | None,
    city: str | None,
    customer_name: str | None,
    previous_hooks: list[str] | None = None,
) -> tuple[str, str]:
    """
    Select a deterministic value-first relance message.

    Args:
        attempt_number: 1, 2, or 3
        language: Customer's preferred language
        product_interest: Retained for compatibility; not used for AI personalization
        city: Retained for compatibility; not used for AI personalization
        customer_name: Retained for compatibility; not used for AI personalization
        previous_hooks: Retained for compatibility; not used for AI personalization

    Returns:
        Tuple of (hook_text, hook_type)
        - hook_text: The relance message to send (2-3 sentences max)
        - hook_type: One of 'reciprocity', 'social_proof', 'scarcity', 'loyalty'

    Raises:
        ValueError: If attempt_number not in 1, 2, 3
    """
    if attempt_number not in [1, 2, 3]:
        raise ValueError(f"Invalid attempt_number: {attempt_number} (must be 1, 2, or 3)")

    # Preserve the public call contract while legacy AI personalization is retired.
    del product_interest, city, customer_name, previous_hooks

    # Determine hook angle based on attempt.
    if attempt_number == 1:
        hook_type = "reciprocity"  # Value reminder
    elif attempt_number == 2:
        hook_type = "social_proof"  # Others are buying
    else:  # attempt_number == 3
        hook_type = "scarcity"  # Final offer

    hook_text = i18n.t(f"relance_fallback_{attempt_number}", language)

    log.info(
        "m6.hooks.selected",
        attempt=attempt_number,
        hook_type=hook_type,
        language=language,
        hook_length=len(hook_text),
    )

    return hook_text, hook_type


def get_fallback_hook(attempt_number: int, language: Language) -> str:
    """
    Get fallback hook — tries static template file first, then i18n.

    Static templates in templates/ are curated and native-reviewed.
    i18n strings serve as last-resort backstop.
    """
    import json
    from pathlib import Path

    lang_name = {
        Language.french: "french",
        Language.lingala: "lingala",
        Language.swahili: "swahili",
    }[language]

    template_path = (
        Path(__file__).parent / "templates" / f"{lang_name}_attempt_{attempt_number}.json"
    )
    if template_path.exists():
        try:
            data = json.loads(template_path.read_text(encoding="utf-8"))
            return data.get("default", "")
        except Exception:
            pass

    key = f"relance_fallback_{attempt_number}"
    return i18n.t(key, language)
