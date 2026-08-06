"""Durable, audited, duplicate-safe internal conversation notes."""

from __future__ import annotations

import hashlib
import hmac
import uuid
from dataclasses import dataclass

from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.conversation import Conversation
from app.models.internal_note import InternalNote
from app.operator_identity.audit import append_operator_audit_event


class InternalNoteError(Exception):
    """Base class for stable internal-note failures."""


class InternalNoteConversationNotFound(InternalNoteError):
    pass


class InternalNoteIdempotencyConflict(InternalNoteError):
    pass


class InternalNoteUnavailable(InternalNoteError):
    pass


@dataclass(frozen=True)
class InternalNoteResult:
    note: InternalNote
    replayed: bool


def _key_digest(secret: str, note_id: uuid.UUID) -> str:
    encoded = secret.encode("utf-8")
    if len(encoded) < 32:
        raise InternalNoteUnavailable("browser idempotency protection is unavailable")
    return hmac.new(
        encoded,
        b"internal-note-key:v1:" + str(note_id).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def _matches_request(
    note: InternalNote,
    *,
    conversation_id: uuid.UUID,
    actor_account_id: uuid.UUID,
    content: str,
) -> bool:
    return (
        note.conversation_id == conversation_id
        and note.author_account_id == actor_account_id
        and note.content == content
    )


async def _existing_result(
    session: AsyncSession,
    *,
    note_id: uuid.UUID,
    conversation_id: uuid.UUID,
    actor_account_id: uuid.UUID,
    content: str,
) -> InternalNoteResult | None:
    existing = await session.get(InternalNote, note_id)
    if existing is None:
        return None
    if not _matches_request(
        existing,
        conversation_id=conversation_id,
        actor_account_id=actor_account_id,
        content=content,
    ):
        raise InternalNoteIdempotencyConflict
    return InternalNoteResult(existing, replayed=True)


async def create_internal_note(
    session: AsyncSession,
    *,
    note_id: uuid.UUID,
    conversation_id: uuid.UUID,
    content: str,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    idempotency_secret: str,
    request_id: str,
    settings: Settings,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> InternalNoteResult:
    """Persist one immutable note and its creation audit in one transaction."""
    digest = _key_digest(idempotency_secret, note_id)
    try:
        existing = await _existing_result(
            session,
            note_id=note_id,
            conversation_id=conversation_id,
            actor_account_id=actor_account_id,
            content=content,
        )
        if existing is not None:
            return existing
        if actor_role not in {"administrator", "operator"}:
            raise InternalNoteConversationNotFound
        if await session.get(Conversation, conversation_id) is None:
            raise InternalNoteConversationNotFound

        note = InternalNote(
            note_id=note_id,
            conversation_id=conversation_id,
            author_account_id=actor_account_id,
            author_display_name=actor_display_name,
            content=content,
        )
        session.add(note)
        await session.flush()
        if note.created_at is None:
            raise InternalNoteUnavailable("authoritative note timestamp is unavailable")
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action="internal_note_created",
            target_type="internal_note",
            target_id=str(note_id),
            reason_code="operator_request",
            outcome="succeeded",
            idempotency_reference=digest,
            metadata={
                "conversation_id": str(conversation_id),
                "character_count": len(content),
                "source": "operator_browser",
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=note.created_at,
            settings=settings,
        )
        await session.commit()
        return InternalNoteResult(note, replayed=False)
    except (InternalNoteConversationNotFound, InternalNoteIdempotencyConflict):
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        try:
            existing = await _existing_result(
                session,
                note_id=note_id,
                conversation_id=conversation_id,
                actor_account_id=actor_account_id,
                content=content,
            )
        except InternalNoteIdempotencyConflict:
            raise
        if existing is not None:
            return existing
        raise InternalNoteUnavailable from exc
    except InternalNoteUnavailable:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise InternalNoteUnavailable from exc
