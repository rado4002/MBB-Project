"""
M1 — Language detector (M2 fast-path implementation).

Strategy:
  1. Keyword fast-path — if ≥2 markers found → confidence ≥ 0.90, no LLM needed.
  2. Claude fallback — for mixed or unclear messages (prompt includes detect instruction).
  3. Sticky — once set on a conversation, never changed within it unless user asks.
  4. Default — French if detection fails entirely.
"""
from __future__ import annotations

import re

# ── Marker dictionaries ────────────────────────────────────────────────────────

_LINGALA_MARKERS = frozenset({
    "mbote", "nalingi", "nazali", "ezali", "ndeko", "tika", "yaka", "te",
    "lokola", "nzoto", "koluka", "biso", "moto", "mingi", "suka", "pete",
    "nakosepela", "nakopesa", "nzela", "eloko", "moninga", "boyei",
})

_SWAHILI_MARKERS = frozenset({
    "habari", "asante", "karibu", "nataka", "nzuri", "sawa", "pole",
    "kweli", "hakuna", "tafadhali", "rafiki", "mzuri", "ningeweza",
    "leo", "kesho", "bei", "bidhaa", "nunua", "wasiliana",
})

_FRENCH_MARKERS = frozenset({
    "bonjour", "merci", "oui", "non", "comment", "prix", "livraison",
    "disponible", "votre", "notre", "voulez", "pouvez", "puis",
    "bonsoir", "vouloir", "acheter", "produit", "combien", "payer",
    "câble", "cable", "telephone", "téléphone",
})

# Opt-out signals across all three languages
_OPT_OUT_SIGNALS: dict[str, list[str]] = {
    "french":  ["stop", "arrête", "arreter", "arrete", "plus de message",
                 "désinscris", "desinscris", "ne plus contacter"],
    "lingala": ["yaka te", "tika", "tika ko-tindela", "nakopesi te"],
    "swahili": ["acha", "simama", "usitume", "usiwasiliane"],
}

_OPT_OUT_FLAT = re.compile(
    "|".join(
        re.escape(sig)
        for signals in _OPT_OUT_SIGNALS.values()
        for sig in signals
    ),
    re.IGNORECASE,
)


def detect_language(text: str, existing_language: str | None = None) -> tuple[str, float]:
    """
    Detect dominant language from text using keyword fast-path.

    Returns:
        (language, confidence) where language ∈ {"lingala", "french", "swahili"}
        and confidence ∈ [0.0, 1.0].
    """
    # Sticky: if conversation already has a detected language, keep it.
    # Caller is responsible for deciding when to override (explicit switch).
    if existing_language:
        return existing_language, 1.0

    words = set(re.findall(r"\b[a-zàâäéèêëîïôùûüçæœ'-]+\b", text.lower()))

    scores: dict[str, int] = {
        "lingala": len(words & _LINGALA_MARKERS),
        "swahili": len(words & _SWAHILI_MARKERS),
        "french":  len(words & _FRENCH_MARKERS),
    }

    best_lang = max(scores, key=scores.__getitem__)
    best_score = scores[best_lang]

    if best_score == 0:
        return "french", 0.3   # Default fallback (FR2)

    total = sum(scores.values())
    confidence = min(0.5 + (best_score / max(total, 1)) * 0.5, 0.99)

    # High-confidence threshold: ≥2 unambiguous markers
    if best_score >= 2:
        confidence = 0.9

    return best_lang, round(confidence, 2)


def is_opt_out(text: str) -> bool:
    """Return True if text contains any known opt-out signal."""
    return bool(_OPT_OUT_FLAT.search(text))


def is_language_switch_request(text: str) -> str | None:
    """
    Detect explicit language switch requests.
    Returns new language code or None.
    """
    lower = text.lower()
    if any(kw in lower for kw in ("en français", "parle français", "in french")):
        return "french"
    if any(kw in lower for kw in ("en lingala", "lingala", "solomela lingala")):
        return "lingala"
    if any(kw in lower for kw in ("kwa kiswahili", "swahili", "sema kiswahili")):
        return "swahili"
    return None
