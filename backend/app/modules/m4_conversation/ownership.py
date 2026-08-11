"""Exclusive, duplicate-safe Human/AI conversation ownership transitions."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters import ai_adapter_eligibility
from app.models.conversation import Conversation
from app.models.conversation_ownership_idempotency import (
    ConversationOwnershipIdempotency,
)
from app.models.operator_account import OperatorAccount
from app.models.escalation_ticket import EscalationTicket
from app.operator_identity.audit import append_operator_audit_event

_RESERVATION_LIFETIME = timedelta(minutes=5)


class OwnershipTransitionError(Exception):
    """Base class for stable ownership-transition failures."""


class ConversationNotFound(OwnershipTransitionError):
    pass


class IdempotencyConflict(OwnershipTransitionError):
    pass


class IdempotencyInProgress(OwnershipTransitionError):
    pass


class OwnershipTransitionUnavailable(OwnershipTransitionError):
    pass


class ReturnToAIDisabled(OwnershipTransitionError):
    pass


class ReturnToAIUnavailable(OwnershipTransitionError):
    pass


@dataclass(frozen=True)
class OwnershipSnapshot:
    conversation_id: uuid.UUID
    owner_type: str
    human_owner_account_id: uuid.UUID | None
    human_owner_display_name: str | None
    ai_execution_state: str
    version: int
    updated_at: datetime


class OwnershipConflict(OwnershipTransitionError):
    def __init__(self, current: OwnershipSnapshot) -> None:
        super().__init__("authoritative ownership changed")
        self.current = current


@dataclass(frozen=True)
class OwnershipTransitionResult:
    ownership: OwnershipSnapshot
    replayed: bool


@dataclass(frozen=True)
class _Reservation:
    record_id: uuid.UUID
    reservation_token: uuid.UUID | None
    key_digest: str
    replayed: bool = False


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _digest(secret: str, purpose: bytes, value: bytes) -> str:
    encoded_secret = secret.encode("utf-8")
    if len(encoded_secret) < 32:
        raise OwnershipTransitionUnavailable(
            "browser idempotency protection is unavailable"
        )
    return hmac.new(encoded_secret, purpose + value, hashlib.sha256).hexdigest()


def _digests(
    *,
    secret: str,
    idempotency_key: uuid.UUID,
    conversation_id: uuid.UUID,
    target_owner_type: str,
    expected_version: int,
) -> tuple[str, str]:
    key_digest = _digest(
        secret,
        b"conversation-ownership-key:v1:",
        str(idempotency_key).encode("ascii"),
    )
    canonical = json.dumps(
        {
            "conversation_id": str(conversation_id),
            "expected_version": expected_version,
            "target_owner_type": target_owner_type,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    return key_digest, _digest(
        secret,
        b"conversation-ownership-request:v1:",
        canonical,
    )


async def _reserve(
    session: AsyncSession,
    *,
    actor_account_id: uuid.UUID,
    conversation_id: uuid.UUID,
    target_owner_type: str,
    expected_version: int,
    key_digest: str,
    request_fingerprint: str,
) -> _Reservation:
    now = _utcnow()
    token = uuid.uuid4()
    await session.commit()
    record_id = await session.scalar(
        postgresql_insert(ConversationOwnershipIdempotency)
        .values(
            actor_account_id=actor_account_id,
            conversation_id=conversation_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            state="in_progress",
            reservation_token=token,
            locked_until=now + _RESERVATION_LIFETIME,
            target_owner_type=target_owner_type,
            expected_version=expected_version,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["actor_account_id", "key_digest"]
        )
        .returning(ConversationOwnershipIdempotency.record_id)
    )
    if record_id is not None:
        await session.commit()
        return _Reservation(record_id, token, key_digest)

    existing = await session.scalar(
        select(ConversationOwnershipIdempotency)
        .where(
            ConversationOwnershipIdempotency.actor_account_id
            == actor_account_id,
            ConversationOwnershipIdempotency.key_digest == key_digest,
        )
        .with_for_update()
    )
    if existing is None:
        await session.rollback()
        raise OwnershipTransitionUnavailable("idempotency reservation disappeared")
    if not hmac.compare_digest(
        existing.request_fingerprint, request_fingerprint
    ):
        await session.commit()
        raise IdempotencyConflict
    if existing.state == "completed":
        await session.commit()
        return _Reservation(existing.record_id, None, key_digest, replayed=True)
    if existing.locked_until is not None and existing.locked_until > now:
        await session.commit()
        raise IdempotencyInProgress
    existing.reservation_token = token
    existing.locked_until = now + _RESERVATION_LIFETIME
    existing.updated_at = now
    await session.commit()
    return _Reservation(existing.record_id, token, key_digest)


async def _discard(
    session: AsyncSession, reservation: _Reservation
) -> None:
    await session.rollback()
    if reservation.reservation_token is None:
        return
    await session.execute(
        delete(ConversationOwnershipIdempotency).where(
            ConversationOwnershipIdempotency.record_id == reservation.record_id,
            ConversationOwnershipIdempotency.state == "in_progress",
            ConversationOwnershipIdempotency.reservation_token
            == reservation.reservation_token,
        )
    )
    await session.commit()


async def _snapshot(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    for_update: bool = False,
) -> OwnershipSnapshot | None:
    statement = (
        select(
            Conversation.conversation_id,
            Conversation.owner_type,
            Conversation.human_owner_account_id,
            OperatorAccount.display_name.label("human_owner_display_name"),
            Conversation.ai_execution_state,
            Conversation.ownership_version,
            Conversation.ownership_updated_at,
        )
        .outerjoin(
            OperatorAccount,
            OperatorAccount.account_id == Conversation.human_owner_account_id,
        )
        .where(Conversation.conversation_id == conversation_id)
    )
    if for_update:
        statement = statement.with_for_update(of=Conversation)
    row = (await session.execute(statement)).mappings().one_or_none()
    if row is None:
        return None
    return OwnershipSnapshot(
        conversation_id=row["conversation_id"],
        owner_type=row["owner_type"],
        human_owner_account_id=row["human_owner_account_id"],
        human_owner_display_name=row["human_owner_display_name"],
        ai_execution_state=row["ai_execution_state"],
        version=row["ownership_version"],
        updated_at=row["ownership_updated_at"],
    )


async def ai_may_reply(
    session: AsyncSession,
    conversation_id: uuid.UUID,
    *,
    lock: bool = False,
    expected_ownership_version: int | None = None,
) -> bool:
    """Read or lock the authoritative gate used by autonomous reply execution."""
    statement = select(
        Conversation.owner_type,
        Conversation.ai_execution_state,
        Conversation.ownership_version,
    ).where(Conversation.conversation_id == conversation_id)
    if lock:
        statement = statement.with_for_update()
    row = (await session.execute(statement)).one_or_none()
    return bool(
        row
        and row[0] == "ai"
        and row[1] == "eligible"
        and (
            expected_ownership_version is None
            or row[2] == expected_ownership_version
        )
    )


async def ai_reply_ownership_version(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> int | None:
    """Return the trusted generation only while AI currently has authority."""
    row = (
        await session.execute(
            select(
                Conversation.owner_type,
                Conversation.ai_execution_state,
                Conversation.ownership_version,
            ).where(Conversation.conversation_id == conversation_id)
        )
    ).one_or_none()
    if row is None or row[0] != "ai" or row[1] != "eligible":
        return None
    return row[2]


async def ai_is_waiting_for_human(
    session: AsyncSession,
    conversation_id: uuid.UUID,
) -> bool:
    row = (
        await session.execute(
            select(
                Conversation.owner_type,
                Conversation.human_owner_account_id,
                Conversation.ai_execution_state,
            ).where(Conversation.conversation_id == conversation_id)
        )
    ).one_or_none()
    return bool(
        row
        and row[0] == "ai"
        and row[1] is None
        and row[2] == "paused"
    )


async def transition_ownership(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    target_owner_type: str,
    expected_version: int,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    idempotency_key: uuid.UUID,
    idempotency_secret: str,
    request_id: str,
    ai_adapter: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OwnershipTransitionResult:
    key_digest, request_fingerprint = _digests(
        secret=idempotency_secret,
        idempotency_key=idempotency_key,
        conversation_id=conversation_id,
        target_owner_type=target_owner_type,
        expected_version=expected_version,
    )
    try:
        if await session.get(Conversation, conversation_id) is None:
            await session.rollback()
            raise ConversationNotFound
        reservation = await _reserve(
            session,
            actor_account_id=actor_account_id,
            conversation_id=conversation_id,
            target_owner_type=target_owner_type,
            expected_version=expected_version,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
    except (ConversationNotFound, IdempotencyConflict, IdempotencyInProgress):
        raise
    except OwnershipTransitionUnavailable:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OwnershipTransitionUnavailable(
            "ownership idempotency persistence is unavailable"
        ) from exc

    if reservation.replayed:
        try:
            current = await _snapshot(session, conversation_id)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            await session.rollback()
            raise OwnershipTransitionUnavailable(
                "committed ownership result is unavailable"
            ) from exc
        if current is None:
            raise OwnershipTransitionUnavailable(
                "committed ownership result is invalid"
            )
        return OwnershipTransitionResult(current, replayed=True)

    try:
        current = await _snapshot(session, conversation_id, for_update=True)
        if current is None:
            await _discard(session, reservation)
            raise ConversationNotFound
        if current.version != expected_version or current.owner_type == target_owner_type:
            await _discard(session, reservation)
            raise OwnershipConflict(current)

        if target_owner_type == "human":
            if current.owner_type != "ai":
                await _discard(session, reservation)
                raise OwnershipConflict(current)
            next_human_owner = actor_account_id
            next_ai_state = "paused"
            action = "conversation_taken_over"
        else:
            if current.owner_type != "human":
                await _discard(session, reservation)
                raise OwnershipConflict(current)
            if (
                current.human_owner_account_id != actor_account_id
                and actor_role != "administrator"
            ):
                await _discard(session, reservation)
                raise OwnershipConflict(current)
            eligibility = ai_adapter_eligibility(ai_adapter)
            if eligibility == "disabled":
                await _discard(session, reservation)
                raise ReturnToAIDisabled
            if eligibility != "eligible":
                await _discard(session, reservation)
                raise ReturnToAIUnavailable
            next_human_owner = None
            next_ai_state = "eligible"
            action = "conversation_returned_to_ai"

        now = _utcnow()
        new_version = current.version + 1
        changed = await session.execute(
            update(Conversation)
            .where(
                Conversation.conversation_id == conversation_id,
                Conversation.ownership_version == expected_version,
                Conversation.owner_type == current.owner_type,
            )
            .values(
                owner_type=target_owner_type,
                human_owner_account_id=next_human_owner,
                ai_execution_state=next_ai_state,
                ownership_version=new_version,
                ownership_updated_at=now,
                updated_at=now,
            )
        )
        if changed.rowcount != 1:
            raise OwnershipTransitionUnavailable(
                "ownership compare-and-set was not applied"
            )
        if target_owner_type == "human":
            await session.execute(
                update(EscalationTicket)
                .where(
                    EscalationTicket.conversation_id == conversation_id,
                    EscalationTicket.source == "ai_capability",
                    EscalationTicket.escalation_type == "human_handoff",
                    EscalationTicket.status == "open",
                )
                .values(status="in_progress")
            )
        else:
            await session.execute(
                update(EscalationTicket)
                .where(
                    EscalationTicket.conversation_id == conversation_id,
                    EscalationTicket.source == "ai_capability",
                    EscalationTicket.escalation_type == "human_handoff",
                    EscalationTicket.status.in_(("open", "in_progress")),
                )
                .values(status="closed")
            )
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action=action,
            target_type="conversation",
            target_id=str(conversation_id),
            reason_code="operator_request",
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "from_owner_type": current.owner_type,
                "to_owner_type": target_owner_type,
                "ownership_version": new_version,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=now,
        )
        completed = await session.execute(
            update(ConversationOwnershipIdempotency)
            .where(
                ConversationOwnershipIdempotency.record_id
                == reservation.record_id,
                ConversationOwnershipIdempotency.state == "in_progress",
                ConversationOwnershipIdempotency.reservation_token
                == reservation.reservation_token,
            )
            .values(
                state="completed",
                reservation_token=None,
                locked_until=None,
                result_version=new_version,
                updated_at=now,
                completed_at=now,
            )
        )
        if completed.rowcount != 1:
            raise OwnershipTransitionUnavailable(
                "ownership idempotency reservation was lost"
            )
        await session.commit()
        result = await _snapshot(session, conversation_id)
        if result is None:
            raise OwnershipTransitionUnavailable(
                "committed ownership result is unavailable"
            )
        return OwnershipTransitionResult(result, replayed=False)
    except (
        ConversationNotFound,
        OwnershipConflict,
        ReturnToAIDisabled,
        ReturnToAIUnavailable,
    ):
        raise
    except OwnershipTransitionUnavailable:
        try:
            await _discard(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError):
            pass
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        try:
            await _discard(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError):
            pass
        raise OwnershipTransitionUnavailable(
            "ownership persistence is unavailable"
        ) from exc
    except Exception:
        try:
            await _discard(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError):
            pass
        raise
