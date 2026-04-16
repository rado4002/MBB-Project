"""
app/modules/m5_qualification/stages.py — StoryBrand-based lead stage progression.

Stage flow:
    AWARENESS → CONSIDERATION → DECISION

Rules:
  - AWARENESS: Customer exploring, not yet qualified (score < 4)
  - CONSIDERATION: Qualified lead (score 4-6), asking questions, comparing
  - DECISION: High intent (score 7+), ready to buy

Transitions are logged in the lead_stage_transitions table for audit trail.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead import Lead

log = structlog.get_logger(__name__)

LeadStage = Literal["awareness", "consideration", "decision"]

# ── Stage transition rules ───────────────────────────────────────────────────

_VALID_TRANSITIONS: dict[LeadStage, set[LeadStage]] = {
    "awareness": {"consideration", "decision"},
    "consideration": {"decision", "awareness"},  # can regress if disengaged
    "decision": {"consideration"},  # can regress if lost urgency
}


def can_transition(from_stage: LeadStage, to_stage: LeadStage) -> bool:
    """Check if a stage transition is valid."""
    return to_stage in _VALID_TRANSITIONS.get(from_stage, set())


async def transition_stage(
    *,
    session: AsyncSession,
    lead: Lead,
    new_stage: LeadStage,
    reason: str,
) -> bool:
    """
    Transition a lead to a new stage with audit logging.

    Args:
        session: AsyncSession for DB operations
        lead: Lead ORM instance
        new_stage: Target stage (awareness | consideration | decision)
        reason: Human-readable reason for transition

    Returns:
        True if transition succeeded, False if invalid or no-op
    """
    old_stage = lead.stage

    # No-op if already in target stage
    if old_stage == new_stage:
        log.debug(
            "m5.stage.noop",
            lead_id=str(lead.lead_id),
            stage=old_stage,
        )
        return False

    # Validate transition
    if not can_transition(old_stage, new_stage):  # type: ignore[arg-type]
        log.warning(
            "m5.stage.invalid_transition",
            lead_id=str(lead.lead_id),
            from_stage=old_stage,
            to_stage=new_stage,
            reason="invalid_transition_path",
        )
        return False

    # Update lead stage
    lead.stage = new_stage
    lead.updated_at = datetime.now(timezone.utc)

    # Log transition (will be persisted when session commits)
    await _log_stage_transition(
        session=session,
        lead_id=lead.lead_id,
        from_stage=old_stage,
        to_stage=new_stage,
        reason=reason,
    )

    log.info(
        "m5.stage.transitioned",
        lead_id=str(lead.lead_id),
        from_stage=old_stage,
        to_stage=new_stage,
        reason=reason,
    )
    return True


async def _log_stage_transition(
    *,
    session: AsyncSession,
    lead_id: uuid.UUID,
    from_stage: str,
    to_stage: str,
    reason: str,
) -> None:
    """
    Create an audit log entry for the stage transition.
    (Requires lead_stage_transitions table — will be created in migration)
    """
    # Import here to avoid circular dependency
    from app.models.lead_stage_transition import LeadStageTransition

    transition = LeadStageTransition(
        transition_id=uuid.uuid4(),
        lead_id=lead_id,
        from_stage=from_stage,
        to_stage=to_stage,
        reason=reason,
        created_at=datetime.now(timezone.utc),
    )
    session.add(transition)
    await session.flush()


def suggest_stage_from_score(score_value: int) -> LeadStage:
    """
    Suggest a lead stage based on score value.

    Rules (aligned with Phase 1.B spec — 0-100 scale):
      - score 70-100  → DECISION (hot, ready to buy)
      - score 40-69   → CONSIDERATION (warm, exploring)
      - score 0-39    → AWARENESS (cold, vague interest)
    """
    if score_value >= 70:
        return "decision"
    elif score_value >= 40:
        return "consideration"
    else:
        return "awareness"


def suggest_stage_from_engagement(
    *,
    message_count: int,
    has_price_inquiry: bool,
    has_product_specificity: bool,
) -> LeadStage:
    """
    Suggest a lead stage based on engagement signals (alternative to score).

    Rules:
      - High engagement (10+ msgs, price inquiry, specific product) → DECISION
      - Medium engagement (5+ msgs, price OR product) → CONSIDERATION
      - Low engagement → AWARENESS
    """
    if message_count >= 10 and has_price_inquiry and has_product_specificity:
        return "decision"
    elif message_count >= 5 and (has_price_inquiry or has_product_specificity):
        return "consideration"
    else:
        return "awareness"
