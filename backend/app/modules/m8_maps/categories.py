"""
MAPS Tag Taxonomy — Category definitions and validation.

Categories:
  product_demand      — What products are people asking about?
  silence_reason      — Why did the customer go silent?
  conversion_trigger  — What made the customer buy?
  language_usage      — Language distribution across convos
  opt_out_reason      — Why did the customer opt out?
"""
from __future__ import annotations

# ── Tag taxonomy: category → valid patterns ───────────────────────────────────
CATEGORY_PATTERNS: dict[str, list[str]] = {
    "product_demand": [
        "product_name",
        "category",
        "price_range",
        "availability_check",
        "comparison",
    ],
    "silence_reason": [
        "price_high",
        "no_stock",
        "slow_reply",
        "competitor",
        "distrust",
        "blackout",
        "not_ready",
        "wrong_product",
    ],
    "conversion_trigger": [
        "promo",
        "recommendation",
        "urgency",
        "social_proof",
        "relance_hook",
        "price_drop",
        "trust_built",
    ],
    "language_usage": [
        "lingala",
        "french",
        "swahili",
        "mixed",
    ],
    "opt_out_reason": [
        "too_many_messages",
        "not_interested",
        "wrong_product",
        "privacy_concern",
        "competitor_chosen",
        "bad_experience",
    ],
}

VALID_CATEGORIES = set(CATEGORY_PATTERNS.keys())


def is_valid_pattern(category: str, pattern: str) -> bool:
    """Check if a pattern is valid for the given category."""
    return category in CATEGORY_PATTERNS and pattern in CATEGORY_PATTERNS[category]


def get_patterns_for_category(category: str) -> list[str]:
    """Return valid patterns for a category, or empty list."""
    return CATEGORY_PATTERNS.get(category, [])
