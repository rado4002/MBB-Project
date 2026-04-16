"""
app/modules/m4_conversation/engine.py — Conversation state machine.

State transitions:
  active     → qualifying   (qualification signals detected)
  qualifying → nurturing    (qualification complete, lead created)
  nurturing  → escalated    (voice note / complex issue / high-value)
  nurturing  → converted    (order placed / payment confirmed)
  escalated  → converted    (resolved by hub team)
  ANY        → dormant      (opt-out keyword OR silent > 14 days)
  dormant    → active       (customer resumes messaging)

All transitions are logged via structured logging for audit trail.
The actual DB update is performed by the caller (Celery task or service layer).
"""
from __future__ import annotations

import structlog

log = structlog.get_logger(__name__)

# ── Valid transitions ─────────────────────────────────────────────────────────

_VALID_TRANSITIONS: dict[str, set[str]] = {
    "active":     {"qualifying", "dormant"},
    "qualifying": {"nurturing", "escalated", "dormant"},
    "nurturing":  {"escalated", "converted", "dormant"},
    "escalated":  {"nurturing", "converted", "dormant"},
    "converted":  {"dormant"},
    "dormant":    {"active"},
}

# ── Qualification signals — keywords that suggest buying intent ───────────────

_QUALIFICATION_SIGNALS: dict[str, list[str]] = {
    "french": [
        "prix", "combien", "coûte", "acheter", "commander", "livraison",
        "disponible", "stock", "produit", "catalogue", "promo",
    ],
    "lingala": [
        "ntalu", "boni", "kosomba", "biloko", "tinda", "catalogue",
        "ko-pesa", "mbongo", "promotion",
    ],
    "swahili": [
        "bei", "kiasi", "nunua", "bidhaa", "oda", "uwasilishaji",
        "stock", "katalogi", "punguzo",
    ],
}

# Flatten for quick lookup
_ALL_SIGNALS = set()
for _signals in _QUALIFICATION_SIGNALS.values():
    _ALL_SIGNALS.update(_signals)


class InvalidTransitionError(ValueError):
    """Raised when a state transition is not allowed."""


def can_transition(current: str, target: str) -> bool:
    """Check if a state transition is valid."""
    return target in _VALID_TRANSITIONS.get(current, set())


def transition(
    current: str,
    target: str,
    *,
    conversation_id: str = "",
    reason: str = "",
) -> str:
    """
    Validate and execute a state transition.
    Returns the new state.
    Raises InvalidTransitionError if the transition is not allowed.
    """
    if not can_transition(current, target):
        raise InvalidTransitionError(
            f"Cannot transition from '{current}' to '{target}'"
        )

    log.info(
        "m4.state_transition",
        conv_id=conversation_id,
        from_state=current,
        to_state=target,
        reason=reason,
    )
    return target


def detect_qualification_signals(text: str) -> bool:
    """
    Check if the message contains buying-intent signals.
    Used to trigger active → qualifying transition.
    """
    words = set(text.lower().split())
    return bool(words & _ALL_SIGNALS)


def should_go_dormant_on_timeout(last_message_days: int, threshold: int = 14) -> bool:
    """Check if a conversation should transition to dormant due to inactivity."""
    return last_message_days >= threshold
