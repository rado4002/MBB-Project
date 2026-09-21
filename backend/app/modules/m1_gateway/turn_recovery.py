"""Durable, explicit recovery decisions for accepted inbound M1 turns."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_turn_audit import AITurnAudit
from app.models.conversation import Conversation
from app.models.inbound_turn_lifecycle import InboundTurnLifecycle
from app.models.message import Message
from app.models.order_draft import OrderDraft


_CLAIM_TTL = timedelta(minutes=30)
_TERMINAL_STATES = {"send_completed", "send_uncertain", "skipped"}


@dataclass(frozen=True)
class TurnClaim:
    action: Literal["process", "resume_send", "noop", "busy"]
    state: str
    ownership_version: int
    outbound_message_id: uuid.UUID | None = None
    outcome_type: str | None = None
    disposition_code: str | None = None


@dataclass(frozen=True)
class RecoveryOutbound:
    source_message_id: uuid.UUID
    conversation_id: uuid.UUID
    customer_phone: str
    text: str
    language: str
    ownership_version: int
    outbound_message_id: uuid.UUID
    outcome_type: str


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def add_pending_turn(
    session: AsyncSession,
    *,
    source_message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    ownership_version: int,
) -> None:
    """Stage lifecycle creation in the same transaction as the inbound row."""
    session.add(
        InboundTurnLifecycle(
            source_message_id=source_message_id,
            conversation_id=conversation_id,
            ownership_version=ownership_version,
            state="pending",
        )
    )


async def _legacy_lifecycle(
    session: AsyncSession,
    source: Message,
) -> InboundTurnLifecycle:
    """Reconcile pre-migration durable effects without inventing domain truth."""
    conversation = await session.get(Conversation, source.conversation_id)
    if conversation is None:
        raise ValueError("inbound conversation is missing")

    audit = await session.scalar(
        select(AITurnAudit)
        .where(
            AITurnAudit.source_message_id == source.message_id,
            AITurnAudit.outbound_message_id.is_not(None),
        )
        .order_by(AITurnAudit.created_at.desc(), AITurnAudit.turn_id.desc())
        .limit(1)
    )
    resolved_draft = await session.scalar(
        select(OrderDraft)
        .where(OrderDraft.resolved_by_message_id == source.message_id)
        .order_by(OrderDraft.resolved_at.desc(), OrderDraft.draft_version.desc())
        .limit(1)
    )
    outcome_type = None
    outbound_message_id = None
    ownership_version = conversation.ownership_version
    if resolved_draft is not None:
        outcome_type = "draft_reply"
        outbound_message_id = resolved_draft.resolution_outbound_message_id
    elif audit is not None and audit.outbound_message_id is not None:
        outcome_type = {
            "fallback_used": "fallback",
            "handoff_requested": "handoff",
            "order_draft_presented": "order_draft",
        }.get(audit.outcome, "response")
        outbound_message_id = audit.outbound_message_id

    state = "outcome_committed" if outbound_message_id is not None else "pending"
    disposition = None
    if resolved_draft is not None and resolved_draft.order_id is not None:
        state = "skipped"
        disposition = "order_committed_no_send"
    elif (
        resolved_draft is not None
        and conversation.ownership_updated_at > resolved_draft.resolved_at
    ):
        state = "skipped"
        disposition = "ownership_changed_before_send"
    elif (
        audit is not None
        and outcome_type != "handoff"
        and conversation.ownership_updated_at > audit.created_at
    ):
        state = "skipped"
        disposition = "ownership_changed_before_send"
    latest_inbound_id = await session.scalar(
        select(Message.message_id)
        .where(
            Message.conversation_id == source.conversation_id,
            Message.direction == "inbound",
        )
        .order_by(
            Message.created_at.desc(),
            Message.timestamp.desc(),
            Message.message_id.desc(),
        )
        .limit(1)
    )
    if state == "pending" and latest_inbound_id != source.message_id:
        state = "skipped"
        disposition = "superseded"
    elif state == "pending" and (
        conversation.owner_type != "ai"
        or conversation.ai_execution_state != "eligible"
    ):
        state = "skipped"
        disposition = "ai_not_eligible"

    lifecycle = InboundTurnLifecycle(
        source_message_id=source.message_id,
        conversation_id=source.conversation_id,
        ownership_version=ownership_version,
        state=state,
        outcome_type=outcome_type,
        outbound_message_id=outbound_message_id,
        disposition_code=disposition,
    )
    session.add(lifecycle)
    await session.flush()
    return lifecycle


async def claim_turn(
    source_message_id: uuid.UUID,
    *,
    attempt_id: str,
) -> TurnClaim:
    """Claim missing work, or identify the exact durable stage to resume."""
    from app.database import async_session_factory

    if not attempt_id or len(attempt_id) > 64:
        raise ValueError("attempt ID must contain 1 to 64 characters")

    for collision_attempt in range(2):
        async with async_session_factory() as session:
            try:
                lifecycle = await session.scalar(
                    select(InboundTurnLifecycle)
                    .where(
                        InboundTurnLifecycle.source_message_id == source_message_id
                    )
                    .with_for_update()
                )
                if lifecycle is None:
                    source = await session.scalar(
                        select(Message).where(
                            Message.message_id == source_message_id,
                            Message.direction == "inbound",
                        )
                    )
                    if source is None:
                        raise ValueError("accepted inbound message is missing")
                    lifecycle = await _legacy_lifecycle(session, source)

                now = _utcnow()
                if lifecycle.state in _TERMINAL_STATES:
                    await session.commit()
                    return TurnClaim(
                        action="noop",
                        state=lifecycle.state,
                        ownership_version=lifecycle.ownership_version,
                        outbound_message_id=lifecycle.outbound_message_id,
                        outcome_type=lifecycle.outcome_type,
                        disposition_code=lifecycle.disposition_code,
                    )

                claim_is_live = (
                    lifecycle.attempt_id is not None
                    and lifecycle.attempt_id != attempt_id
                    and lifecycle.claim_expires_at is not None
                    and lifecycle.claim_expires_at > now
                )
                if claim_is_live:
                    await session.commit()
                    return TurnClaim(
                        action="busy",
                        state=lifecycle.state,
                        ownership_version=lifecycle.ownership_version,
                        outbound_message_id=lifecycle.outbound_message_id,
                        outcome_type=lifecycle.outcome_type,
                    )

                action: Literal["process", "resume_send"]
                if lifecycle.state == "outcome_committed":
                    action = "resume_send"
                else:
                    lifecycle.state = "processing"
                    action = "process"
                lifecycle.attempt_id = attempt_id
                lifecycle.claim_expires_at = now + _CLAIM_TTL
                lifecycle.updated_at = now
                await session.commit()
                return TurnClaim(
                    action=action,
                    state=lifecycle.state,
                    ownership_version=lifecycle.ownership_version,
                    outbound_message_id=lifecycle.outbound_message_id,
                    outcome_type=lifecycle.outcome_type,
                )
            except IntegrityError:
                await session.rollback()
                if collision_attempt == 0:
                    continue
                raise
    raise AssertionError("turn claim collision retry did not terminate")


async def source_is_latest(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    source_message_id: uuid.UUID,
) -> bool:
    latest = await session.scalar(
        select(Message.message_id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
        )
        .order_by(
            Message.created_at.desc(),
            Message.timestamp.desc(),
            Message.message_id.desc(),
        )
        .limit(1)
    )
    return latest == source_message_id


async def load_recovery_outbound(
    source_message_id: uuid.UUID,
) -> RecoveryOutbound | None:
    from app.database import async_session_factory

    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(InboundTurnLifecycle, Message, Conversation)
                .join(
                    Message,
                    Message.message_id
                    == InboundTurnLifecycle.outbound_message_id,
                )
                .join(
                    Conversation,
                    Conversation.conversation_id
                    == InboundTurnLifecycle.conversation_id,
                )
                .where(
                    InboundTurnLifecycle.source_message_id == source_message_id,
                    InboundTurnLifecycle.state == "outcome_committed",
                )
            )
        ).one_or_none()
        if row is None:
            return None
        lifecycle, outbound, conversation = row
        if lifecycle.outcome_type is None:
            return None
        return RecoveryOutbound(
            source_message_id=source_message_id,
            conversation_id=lifecycle.conversation_id,
            customer_phone=conversation.customer_id,
            text=outbound.content,
            language=outbound.language,
            ownership_version=lifecycle.ownership_version,
            outbound_message_id=outbound.message_id,
            outcome_type=lifecycle.outcome_type,
        )


async def mark_outcome_committed(
    session: AsyncSession,
    *,
    source_message_id: uuid.UUID | None,
    outbound_message_id: uuid.UUID,
    outcome_type: str,
) -> bool:
    """Attach a committed output inside its existing domain transaction."""
    if source_message_id is None:
        return False
    if not callable(getattr(session, "get", None)):
        return False
    lifecycle = await session.get(
        InboundTurnLifecycle,
        source_message_id,
        with_for_update=True,
    )
    if lifecycle is None:
        return False
    if lifecycle.state in _TERMINAL_STATES:
        return False
    lifecycle.state = "outcome_committed"
    lifecycle.outbound_message_id = outbound_message_id
    lifecycle.outcome_type = outcome_type
    lifecycle.disposition_code = None
    lifecycle.attempt_id = None
    lifecycle.claim_expires_at = None
    lifecycle.updated_at = _utcnow()
    await session.flush()
    return True


async def mark_skipped_in_session(
    session: AsyncSession,
    *,
    source_message_id: uuid.UUID,
    disposition_code: str,
) -> bool:
    if not callable(getattr(session, "get", None)):
        return False
    lifecycle = await session.get(
        InboundTurnLifecycle,
        source_message_id,
        with_for_update=True,
    )
    if lifecycle is None or lifecycle.state in {"send_completed", "send_uncertain"}:
        return False
    lifecycle.state = "skipped"
    lifecycle.disposition_code = disposition_code
    lifecycle.attempt_id = None
    lifecycle.claim_expires_at = None
    lifecycle.updated_at = _utcnow()
    await session.flush()
    return True


async def mark_skipped(
    source_message_id: uuid.UUID,
    *,
    disposition_code: str,
) -> bool:
    from app.database import async_session_factory

    async with async_session_factory() as session:
        changed = await mark_skipped_in_session(
            session,
            source_message_id=source_message_id,
            disposition_code=disposition_code,
        )
        await session.commit()
        return changed


async def mark_send_state(
    source_message_id: uuid.UUID,
    *,
    state: Literal["send_completed", "send_uncertain"],
) -> bool:
    from app.database import async_session_factory

    async with async_session_factory() as session:
        lifecycle = await session.get(
            InboundTurnLifecycle,
            source_message_id,
            with_for_update=True,
        )
        if lifecycle is None or lifecycle.outbound_message_id is None:
            await session.rollback()
            return False
        if lifecycle.state in {"send_completed", "skipped"}:
            await session.rollback()
            return lifecycle.state == state
        lifecycle.state = state
        lifecycle.attempt_id = None
        lifecycle.claim_expires_at = None
        lifecycle.updated_at = _utcnow()
        await session.commit()
        return True
