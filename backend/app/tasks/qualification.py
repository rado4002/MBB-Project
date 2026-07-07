"""
app/tasks/qualification.py — Celery tasks for M3 Lead Qualification.

Tasks:
  - score_conversation: Analyze conversation and create lead if qualified
"""
from __future__ import annotations

import uuid

import structlog
from celery import Task

from app.database import AsyncSessionLocal
from app.models.conversation import Conversation
from app.models.lead import Lead
from app.modules.m5_qualification.service import qualify_and_create_lead
from app.tasks.celery_app import celery_app, run_async

log = structlog.get_logger(__name__)


class _BaseTask(Task):
    """Base task with retry defaults for qualification operations."""

    abstract = True
    max_retries = 3
    default_retry_delay = 60  # seconds


# ── Task: Score conversation and create lead if qualified ────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.qualification.score_conversation",
    queue="default",
    acks_late=True,
)
def score_conversation(
    self: Task,
    conversation_id: str,
    message_text: str,
    msg_count: int,
    response_speed_s: float | None = None,
) -> dict:
    """
    Analyze conversation content and create a Lead if qualification threshold met.

    Triggered by M1 after processing inbound message. Uses weighted scoring:
      - Product specificity: 30%
      - Response speed: 25%
      - Price inquiry: 20%
      - City mentioned: 15%
      - Engagement depth: 10%

    Leads are created if score ≥ 40 (warm or hot).

    Args:
        conversation_id: UUID of Conversation
        message_text: Customer message content
        msg_count: Number of messages in conversation
        response_speed_s: Time since last bot message (seconds)

    Returns:
        Dict with {lead_created: bool, lead_id: str | None, score: str, score_value: int}
    """
    log.info(
        "qualification.score.start",
        conversation_id=conversation_id,
        msg_count=msg_count,
    )

    try:
        result = run_async(
            _score_and_qualify(
                conversation_id=conversation_id,
                message_text=message_text,
                msg_count=msg_count,
                response_speed_s=response_speed_s,
            )
        )
        log.info(
            "qualification.score.complete",
            conversation_id=conversation_id,
            lead_created=result["lead_created"],
            score=result.get("score"),
            score_value=result.get("score_value"),
        )
        return result
    except Exception as exc:
        log.error(
            "qualification.score.error",
            conversation_id=conversation_id,
            error=str(exc),
        )
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _score_and_qualify(
    conversation_id: str,
    message_text: str,
    msg_count: int,
    response_speed_s: float | None,
) -> dict:
    """
    Core logic for scoring conversation and creating lead.

    Returns:
        Dict with lead creation status and scoring details
    """
    conv_uuid = uuid.UUID(conversation_id)

    async with AsyncSessionLocal() as session:
        # Check if lead already exists (idempotency)
        from sqlalchemy import select

        existing_lead = await session.execute(
            select(Lead).where(Lead.conversation_id == conv_uuid)
        )
        if existing_lead.scalar_one_or_none() is not None:
            log.debug(
                "qualification.lead_already_exists",
                conversation_id=conversation_id,
            )
            # Return existing lead
            lead = existing_lead.scalar_one()
            return {
                "lead_created": False,
                "lead_id": str(lead.lead_id),
                "score": lead.score,
                "score_value": lead.score_value,
                "reason": "already_exists",
            }

        # Fetch conversation
        conversation = await session.get(Conversation, conv_uuid)
        if not conversation:
            log.error(
                "qualification.conversation_not_found",
                conversation_id=conversation_id,
            )
            return {
                "lead_created": False,
                "lead_id": None,
                "score": None,
                "score_value": 0,
                "reason": "conversation_not_found",
            }

        # Score and create lead via service layer
        lead = await qualify_and_create_lead(
            session=session,
            customer_phone=conversation.customer_id,
            conversation_id=conv_uuid,
            message_text=message_text,
            msg_count=msg_count,
            response_speed_s=response_speed_s,
        )

        if lead:
            await session.commit()
            log.info(
                "qualification.lead_created",
                lead_id=str(lead.lead_id),
                conversation_id=conversation_id,
                score=lead.score,
                score_value=lead.score_value,
            )

            # Dispatch CRM sync (best-effort, already queued by service layer)
            return {
                "lead_created": True,
                "lead_id": str(lead.lead_id),
                "score": lead.score,
                "score_value": lead.score_value,
                "stage": lead.stage,
                "intent": lead.intent,
            }
        else:
            # Below threshold (score < 40)
            log.debug(
                "qualification.below_threshold",
                conversation_id=conversation_id,
            )
            return {
                "lead_created": False,
                "lead_id": None,
                "score": "cold",
                "score_value": 0,
                "reason": "below_threshold",
            }


# ── Task: Re-score existing lead with new signals ────────────────────────────

@celery_app.task(
    bind=True,
    base=_BaseTask,
    name="app.tasks.qualification.rescore_lead",
    queue="default",
    acks_late=True,
)
def rescore_lead(
    self: Task,
    lead_id: str,
    new_message_text: str,
    engagement_signal: str | None = None,
) -> dict:
    """
    Re-score an existing lead based on new engagement signal.

    Called when customer sends additional messages after lead creation.

    Args:
        lead_id: UUID of Lead
        new_message_text: Latest customer message
        engagement_signal: Optional signal type (price_inquiry, urgency, etc.)

    Returns:
        Dict with {score_updated: bool, old_score: int, new_score: int, stage: str}
    """
    log.info("qualification.rescore.start", lead_id=lead_id)

    try:
        result = run_async(
            _rescore_lead(
                lead_id=lead_id,
                new_message_text=new_message_text,
                engagement_signal=engagement_signal,
            )
        )
        log.info(
            "qualification.rescore.complete",
            lead_id=lead_id,
            score_updated=result["score_updated"],
            new_score=result.get("new_score"),
        )
        return result
    except Exception as exc:
        log.error("qualification.rescore.error", lead_id=lead_id, error=str(exc))
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _rescore_lead(
    lead_id: str,
    new_message_text: str,
    engagement_signal: str | None,
) -> dict:
    """
    Core logic for re-scoring lead.

    Returns:
        Dict with scoring update details
    """
    from app.modules.m5_qualification.scorer import score_lead
    from datetime import datetime, timezone

    lead_uuid = uuid.UUID(lead_id)

    async with AsyncSessionLocal() as session:
        lead = await session.get(Lead, lead_uuid)
        if not lead:
            log.error("qualification.rescore.lead_not_found", lead_id=lead_id)
            return {
                "score_updated": False,
                "reason": "lead_not_found",
            }

        old_score_value = lead.score_value
        old_score = lead.score

        # Calculate new score
        new_score_value, new_score_label, intent = score_lead(
            new_message_text,
            msg_count=1,  # Additional message
            response_speed_s=None,
        )

        # Add bonus points for sustained engagement
        if engagement_signal == "price_inquiry":
            new_score_value += 10
        elif engagement_signal == "urgency":
            new_score_value += 15

        new_score_value = min(new_score_value + old_score_value, 100)

        # Update lead score (prefer higher score)
        if new_score_value > old_score_value:
            lead.score_value = new_score_value

            # Update label
            if new_score_value >= 70:
                lead.score = "hot"
            elif new_score_value >= 40:
                lead.score = "warm"
            else:
                lead.score = "cold"

            # Auto-advance stage if score increased significantly
            if new_score_value >= 70 and lead.stage == "awareness":
                lead.stage = "decision"
                log.info(
                    "qualification.stage_auto_advanced",
                    lead_id=lead_id,
                    new_stage="decision",
                )
            elif new_score_value >= 55 and lead.stage == "awareness":
                lead.stage = "consideration"
                log.info(
                    "qualification.stage_auto_advanced",
                    lead_id=lead_id,
                    new_stage="consideration",
                )

            lead.updated_at = datetime.now(timezone.utc)
            await session.commit()

            return {
                "score_updated": True,
                "old_score": old_score_value,
                "new_score": new_score_value,
                "score_label": lead.score,
                "stage": lead.stage,
            }
        else:
            # No change needed
            return {
                "score_updated": False,
                "old_score": old_score_value,
                "new_score": old_score_value,
                "reason": "no_increase",
            }
