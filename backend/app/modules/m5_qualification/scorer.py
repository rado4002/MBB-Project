"""
app/modules/m5_qualification/scorer.py — Lead scoring engine (Phase 1.B weighted).

Scoring rules (0–100 scale, weighted signals):
  Signal                  Weight  Contribution
  ────────────────────────────────────────────
  Product specificity     30%     0-30 points
  Response speed          25%     0-25 points
  Price inquiry           20%     0-20 points
  City mentioned          15%     0-15 points
  Engagement depth        10%     0-10 points
  ────────────────────────────────────────────
  TOTAL                   100%    0-100 points

Score labels:
  hot  : score ≥ 70  (strong intent, ready to buy)
  warm : score 40-69 (exploring, qualified)
  cold : score < 40  (vague, low intent)
"""
from __future__ import annotations

import re

import structlog

log = structlog.get_logger(__name__)

# ── Scoring keywords ─────────────────────────────────────────────────────────

_PRODUCT_KEYWORDS = frozenset({
    "câble", "cable", "téléphone", "telephone", "chargeur", "écouteur",
    "accessoire", "coque", "batterie", "écran", "screen", "laptop",
    "tablette", "montre", "watch", "speaker", "enceinte", "hdmi",
    "usb", "type-c", "adapter", "adaptateur", "powerbank",
})

_PRICE_KEYWORDS = frozenset({
    "prix", "combien", "ntalu", "boni", "bei", "kiasi", "coûte", "coute",
    "tarif", "promotion", "promo", "remise", "punguzo", "coût", "montant",
})

_BUY_INTENT_KEYWORDS = frozenset({
    "acheter", "commander", "kosomba", "ko-tinda", "nunua", "oda",
    "je veux", "nalingi", "nataka", "livrer", "livraison", "delivery",
    "kupeleka", "ko-mem", "réserver", "weka", "payer", "ko-futa",
})

_CITY_KEYWORDS = frozenset({
    "kinshasa", "lubumbashi", "mbuji-mayi", "kisangani", "kananga",
    "goma", "bukavu", "matadi", "kolwezi", "likasi", "kin", "lushi",
})


def score_lead(
    text: str,
    *,
    msg_count: int = 1,
    response_speed_s: float | None = None,
) -> tuple[int, str, str]:
    """
    Score a lead based on message content and engagement signals (Phase 1.B weighted).

    Args:
        text: Customer message content
        msg_count: Number of messages in conversation
        response_speed_s: Time elapsed since last bot message (seconds)

    Returns:
        (score_value, score_label, intent)
        score_value: 0–100
        score_label: 'hot' | 'warm' | 'cold'
        intent: detected intent string (comma-separated)
    """
    lower = text.lower()
    words = set(re.findall(r"\b[a-zàâäéèêëîïôùûüçæœ'-]+\b", lower))

    score = 0.0
    intent_parts: list[str] = []

    # ── 1. Product specificity (30% weight, max 30 points) ──────────────────
    product_matches = words & _PRODUCT_KEYWORDS
    if product_matches:
        # More specific product keywords → higher score
        specificity_score = min(len(product_matches) * 10, 30)
        score += specificity_score
        intent_parts.append("product_specific")

    # ── 2. Response speed (25% weight, max 25 points) ───────────────────────
    if response_speed_s is not None:
        if response_speed_s < 60:  # < 1 min = hot
            score += 25
        elif response_speed_s < 300:  # < 5 min = warm
            score += 15
        elif response_speed_s < 3600:  # < 1 hour = lukewarm
            score += 5

    # ── 3. Price inquiry (20% weight, max 20 points) ────────────────────────
    if words & _PRICE_KEYWORDS:
        score += 20
        intent_parts.append("price_inquiry")

    # ── 4. City mentioned (15% weight, max 15 points) ───────────────────────
    if words & _CITY_KEYWORDS:
        score += 15
        intent_parts.append("city_mentioned")

    # ── 5. Engagement depth (10% weight, max 10 points) ─────────────────────
    if msg_count >= 10:
        score += 10
    elif msg_count >= 5:
        score += 7
    elif msg_count >= 3:
        score += 3

    # Buy intent detection (bonus signal for intent classification, not scored)
    if words & _BUY_INTENT_KEYWORDS:
        intent_parts.append("buy_intent")

    # Cap at 100
    score = min(int(score), 100)

    # Map to label (aligned with Phase 1.B spec)
    if score >= 70:
        label = "hot"
    elif score >= 40:
        label = "warm"
    else:
        label = "cold"

    intent = ",".join(intent_parts) if intent_parts else "general_inquiry"

    log.info(
        "m5.scored",
        score_value=score,
        score_label=label,
        intent=intent,
        msg_count=msg_count,
        response_speed_s=response_speed_s,
    )
    return score, label, intent
