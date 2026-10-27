"""Human-owned conversation reply acceptance and durable idempotency."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.message import Message
from app.models.order_draft import OrderDraft
from app.modules.m4_conversation.reply_policy import REPLY_ELIGIBLE_STATUSES
from app.modules.m7_conversion.order_drafts import (
    OperatorOrderDraftProposalStale,
    validate_operator_order_draft_proposal,
)


class OperatorReplyError(Exception):
    """Base class for stable operator-reply failures."""


class ReplyConversationNotFound(OperatorReplyError):
    pass


class ReplyIdempotencyConflict(OperatorReplyError):
    pass


class ReplyOwnershipConflict(OperatorReplyError):
    pass


class ReplyOwnershipVersionConflict(OperatorReplyError):
    pass


class ReplyNotEligible(OperatorReplyError):
    pass


class ReplyChannelUnsupported(OperatorReplyError):
    pass


class ReplyAcceptanceUnavailable(OperatorReplyError):
    pass


class ReplyOrderDraftProposalStale(OperatorReplyError):
    pass


@dataclass(frozen=True)
class OperatorReplyResult:
    message: Message
    replayed: bool


def _matches_request(
    message: Message,
    *,
    conversation_id: uuid.UUID,
    actor_account_id: uuid.UUID,
    text: str,
    expected_ownership_version: int,
) -> bool:
    return (
        message.conversation_id == conversation_id
        and message.operator_author_account_id == actor_account_id
        and message.content == text
        and message.accepted_ownership_version == expected_ownership_version
        and message.direction == "outbound"
        and message.content_type == "text"
    )


async def _matches_proposal_binding(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    order_draft_id: uuid.UUID | None,
    order_draft_version: int | None,
) -> bool:
    if order_draft_id is None or order_draft_version is None:
        linked = await session.scalar(
            select(OrderDraft.draft_id).where(
                OrderDraft.presented_outbound_message_id == message_id
            )
        )
        return linked is None
    draft = await session.get(OrderDraft, (order_draft_id, order_draft_version))
    return bool(
        draft is not None
        and draft.presented_outbound_message_id == message_id
    )


async def _existing_result(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_account_id: uuid.UUID,
    text: str,
    expected_ownership_version: int,
    order_draft_id: uuid.UUID | None,
    order_draft_version: int | None,
) -> OperatorReplyResult | None:
    existing = await session.get(Message, message_id)
    if existing is None:
        return None
    if not _matches_request(
        existing,
        conversation_id=conversation_id,
        actor_account_id=actor_account_id,
        text=text,
        expected_ownership_version=expected_ownership_version,
    ):
        raise ReplyIdempotencyConflict
    if not await _matches_proposal_binding(
        session,
        message_id=message_id,
        order_draft_id=order_draft_id,
        order_draft_version=order_draft_version,
    ):
        raise ReplyIdempotencyConflict
    return OperatorReplyResult(existing, replayed=True)


async def accept_operator_reply(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    text: str,
    expected_ownership_version: int,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    message_id: uuid.UUID,
    messaging_adapter: str,
    whatsapp_mode: str,
    order_draft_id: uuid.UUID | None = None,
    order_draft_version: int | None = None,
) -> OperatorReplyResult:
    """Persist one Human-authored reply before broker publication."""
    try:
        existing = await _existing_result(
            session,
            message_id=message_id,
            conversation_id=conversation_id,
            actor_account_id=actor_account_id,
            text=text,
            expected_ownership_version=expected_ownership_version,
            order_draft_id=order_draft_id,
            order_draft_version=order_draft_version,
        )
        if existing is not None:
            return existing

        if messaging_adapter != "whatsapp" or whatsapp_mode != "baileys":
            raise ReplyChannelUnsupported
        if actor_role not in {"administrator", "operator"}:
            raise ReplyConversationNotFound

        conversation = await session.scalar(
            select(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .with_for_update()
        )
        if conversation is None:
            raise ReplyConversationNotFound

        # A same-key request for another conversation can commit while this
        # conversation lock is acquired, so check the global message UUID again.
        existing = await _existing_result(
            session,
            message_id=message_id,
            conversation_id=conversation_id,
            actor_account_id=actor_account_id,
            text=text,
            expected_ownership_version=expected_ownership_version,
            order_draft_id=order_draft_id,
            order_draft_version=order_draft_version,
        )
        if existing is not None:
            return existing

        if (
            conversation.owner_type != "human"
            or conversation.human_owner_account_id != actor_account_id
            or conversation.ai_execution_state != "paused"
        ):
            raise ReplyOwnershipConflict
        if conversation.ownership_version != expected_ownership_version:
            raise ReplyOwnershipVersionConflict
        if conversation.status not in REPLY_ELIGIBLE_STATUSES:
            raise ReplyNotEligible

        proposal = None
        if order_draft_id is not None and order_draft_version is not None:
            try:
                proposal = await validate_operator_order_draft_proposal(
                    session,
                    draft_id=order_draft_id,
                    draft_version=order_draft_version,
                    conversation_id=conversation_id,
                    expected_ownership_version=expected_ownership_version,
                    actor_account_id=actor_account_id,
                    language=conversation.language_detected,
                    text=text,
                )
            except OperatorOrderDraftProposalStale as exc:
                raise ReplyOrderDraftProposalStale from exc

        now = datetime.now(timezone.utc)
        message = Message(
            message_id=message_id,
            conversation_id=conversation_id,
            timestamp=now,
            direction="outbound",
            content=text,
            content_type="text",
            language=conversation.language_detected,
            operator_author_account_id=actor_account_id,
            author_display_name=actor_display_name,
            accepted_ownership_version=expected_ownership_version,
            delivery_state=None,
            delivery_state_timestamp=None,
            created_at=now,
        )
        session.add(message)
        await session.flush()
        if proposal is not None:
            proposal.presented_outbound_message_id = message.message_id
            await session.flush()
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation_id)
            .values(
                message_count=Conversation.message_count + 1,
                last_message_time=now,
                updated_at=now,
            )
        )
        await session.commit()
        return OperatorReplyResult(message, replayed=False)
    except (
        ReplyConversationNotFound,
        ReplyIdempotencyConflict,
        ReplyOwnershipConflict,
        ReplyOwnershipVersionConflict,
        ReplyNotEligible,
        ReplyChannelUnsupported,
        ReplyOrderDraftProposalStale,
    ):
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        try:
            existing = await _existing_result(
                session,
                message_id=message_id,
                conversation_id=conversation_id,
                actor_account_id=actor_account_id,
                text=text,
                expected_ownership_version=expected_ownership_version,
                order_draft_id=order_draft_id,
                order_draft_version=order_draft_version,
            )
        except ReplyIdempotencyConflict:
            raise
        if existing is not None:
            return existing
        raise ReplyAcceptanceUnavailable from exc
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise ReplyAcceptanceUnavailable from exc


async def mark_operator_reply_accepted(
    session: AsyncSession, message_id: uuid.UUID
) -> Message:
    """Record broker acceptance without overwriting a faster worker outcome."""
    try:
        message = await session.get(Message, message_id)
        if message is None or message.operator_author_account_id is None:
            raise ReplyAcceptanceUnavailable
        if message.delivery_state is None:
            now = datetime.now(timezone.utc)
            await session.execute(
                update(Message)
                .where(
                    Message.message_id == message_id,
                    Message.delivery_state.is_(None),
                )
                .values(
                    delivery_state="accepted",
                    delivery_state_timestamp=now,
                )
            )
            await session.commit()
            await session.refresh(message)
        return message
    except ReplyAcceptanceUnavailable:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise ReplyAcceptanceUnavailable from exc
