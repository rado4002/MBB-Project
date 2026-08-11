"""Atomic AI-requested Human attention without Human assignment."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.escalation_ticket import EscalationTicket

_ACTIVE_ESCALATION_STATUSES = ("open", "in_progress")
_AI_HANDOFF_SOURCE = "ai_capability"
_AI_HANDOFF_TYPE = "human_handoff"


class AIHandoffError(Exception):
    """Base class for stable AI handoff failures."""


class AIHandoffConversationNotFound(AIHandoffError):
    pass


class StaleAIAuthority(AIHandoffError):
    pass


class AIHandoffUnavailable(AIHandoffError):
    pass


@dataclass(frozen=True)
class AIHandoffResult:
    conversation_id: uuid.UUID
    ownership_version: int
    escalation_ticket_id: uuid.UUID
    escalation_source: str
    replayed: bool


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _active_ticket(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> EscalationTicket | None:
    return await session.scalar(
        select(EscalationTicket)
        .where(
            EscalationTicket.conversation_id == conversation_id,
            EscalationTicket.status.in_(_ACTIVE_ESCALATION_STATUSES),
        )
        .with_for_update()
    )


async def request_human_handoff(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    expected_ownership_version: int,
) -> AIHandoffResult:
    """Pause AI and ensure one visible Human-attention record atomically."""
    for attempt in range(2):
        try:
            conversation = await session.scalar(
                select(Conversation)
                .where(Conversation.conversation_id == conversation_id)
                .with_for_update()
            )
            if conversation is None:
                await session.rollback()
                raise AIHandoffConversationNotFound

            active_ticket = await _active_ticket(session, conversation_id)
            already_waiting = (
                conversation.owner_type == "ai"
                and conversation.human_owner_account_id is None
                and conversation.ai_execution_state == "paused"
                and active_ticket is not None
            )
            if already_waiting:
                result = AIHandoffResult(
                    conversation_id=conversation_id,
                    ownership_version=conversation.ownership_version,
                    escalation_ticket_id=active_ticket.ticket_id,
                    escalation_source=active_ticket.source,
                    replayed=True,
                )
                await session.commit()
                return result

            has_active_ai_authority = (
                conversation.owner_type == "ai"
                and conversation.human_owner_account_id is None
                and conversation.ai_execution_state == "eligible"
            )
            if (
                not has_active_ai_authority
                or conversation.ownership_version != expected_ownership_version
            ):
                await session.rollback()
                raise StaleAIAuthority

            now = _utcnow()
            if active_ticket is None:
                active_ticket = EscalationTicket(
                    conversation_id=conversation_id,
                    customer_id=conversation.customer_id,
                    priority="medium",
                    reason=_AI_HANDOFF_TYPE,
                    source=_AI_HANDOFF_SOURCE,
                    escalation_type=_AI_HANDOFF_TYPE,
                    operator_reason=None,
                    created_by_account_id=None,
                    status="open",
                    transcript_snapshot=[],
                    created_at=now,
                )
                session.add(active_ticket)

            conversation.ai_execution_state = "paused"
            conversation.ownership_version += 1
            conversation.ownership_updated_at = now
            conversation.updated_at = now
            await session.flush()
            result = AIHandoffResult(
                conversation_id=conversation_id,
                ownership_version=conversation.ownership_version,
                escalation_ticket_id=active_ticket.ticket_id,
                escalation_source=active_ticket.source,
                replayed=False,
            )
            await session.commit()
            return result
        except (AIHandoffConversationNotFound, StaleAIAuthority):
            raise
        except IntegrityError as exc:
            await session.rollback()
            if attempt == 0:
                continue
            raise AIHandoffUnavailable(
                "AI handoff concurrency could not be settled"
            ) from exc
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            await session.rollback()
            raise AIHandoffUnavailable("AI handoff persistence is unavailable") from exc

    raise AIHandoffUnavailable("AI handoff could not be completed")
