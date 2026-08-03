"""Duplicate-safe browser operator escalation creation."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.escalation_ticket import EscalationTicket
from app.models.operator_escalation_idempotency import (
    OperatorEscalationIdempotency,
)
from app.operator_identity.audit import append_operator_audit_event

_ACTIVE_STATUSES = ("open", "in_progress")
_RESERVATION_LIFETIME = timedelta(minutes=5)
_SOURCE = "operator_browser"
_LEGACY_REASON_BY_TYPE = {
    "voice_note": "voice_note",
    "complex_issue": "complex_complaint",
    "high_value_lead": "high_value_lead",
    "payment_issue": "sav_issue",
}
_ACTIVE_UNIQUE_CONSTRAINT = "uq_escalation_tickets_one_active_conversation"


class OperatorEscalationError(Exception):
    """Base class for stable operator escalation failures."""


class ConversationNotFound(OperatorEscalationError):
    pass


class IdempotencyConflict(OperatorEscalationError):
    pass


class IdempotencyInProgress(OperatorEscalationError):
    pass


class EscalationAlreadyOpen(OperatorEscalationError):
    pass


class OperatorEscalationUnavailable(OperatorEscalationError):
    pass


@dataclass(frozen=True)
class OperatorEscalationResult:
    ticket: EscalationTicket
    actor_display_name: str
    replayed: bool


@dataclass(frozen=True)
class _Reservation:
    record_id: uuid.UUID
    reservation_token: uuid.UUID | None
    key_digest: str
    replay_ticket_id: uuid.UUID | None = None

    @property
    def replayed(self) -> bool:
        return self.replay_ticket_id is not None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _hmac_digest(secret: str, purpose: bytes, value: bytes) -> str:
    encoded_secret = secret.encode("utf-8")
    if len(encoded_secret) < 32:
        raise OperatorEscalationUnavailable(
            "browser idempotency protection is unavailable"
        )
    return hmac.new(encoded_secret, purpose + value, hashlib.sha256).hexdigest()


def _digests(
    *,
    secret: str,
    idempotency_key: uuid.UUID,
    conversation_id: uuid.UUID,
    reason: str,
    escalation_type: str,
    priority: str,
) -> tuple[str, str]:
    key_digest = _hmac_digest(
        secret,
        b"operator-escalation-key:v1:",
        str(idempotency_key).encode("ascii"),
    )
    canonical_request = json.dumps(
        {
            "conversation_id": str(conversation_id),
            "priority": priority,
            "reason": reason,
            "source": _SOURCE,
            "type": escalation_type,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request_fingerprint = _hmac_digest(
        secret,
        b"operator-escalation-request:v1:",
        canonical_request,
    )
    return key_digest, request_fingerprint


async def _reserve(
    session: AsyncSession,
    *,
    actor_account_id: uuid.UUID,
    key_digest: str,
    request_fingerprint: str,
) -> _Reservation:
    now = _utcnow()
    reservation_token = uuid.uuid4()
    locked_until = now + _RESERVATION_LIFETIME

    # Browser authentication has already read the account with this session.
    # End that read transaction before committing the short durable reservation.
    await session.commit()
    statement = (
        postgresql_insert(OperatorEscalationIdempotency)
        .values(
            actor_account_id=actor_account_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            state="in_progress",
            reservation_token=reservation_token,
            locked_until=locked_until,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["actor_account_id", "key_digest"]
        )
        .returning(OperatorEscalationIdempotency.record_id)
    )
    record_id = await session.scalar(statement)
    if record_id is not None:
        await session.commit()
        return _Reservation(
            record_id=record_id,
            reservation_token=reservation_token,
            key_digest=key_digest,
        )

    existing = await session.scalar(
        select(OperatorEscalationIdempotency)
        .where(
            OperatorEscalationIdempotency.actor_account_id == actor_account_id,
            OperatorEscalationIdempotency.key_digest == key_digest,
        )
        .with_for_update()
    )
    if existing is None:
        await session.rollback()
        raise OperatorEscalationUnavailable("idempotency reservation disappeared")
    if not hmac.compare_digest(
        existing.request_fingerprint, request_fingerprint
    ):
        await session.commit()
        raise IdempotencyConflict
    if existing.state == "completed":
        replay_ticket_id = existing.ticket_id
        await session.commit()
        if replay_ticket_id is None:
            raise OperatorEscalationUnavailable(
                "completed idempotency result is invalid"
            )
        return _Reservation(
            record_id=existing.record_id,
            reservation_token=None,
            key_digest=key_digest,
            replay_ticket_id=replay_ticket_id,
        )
    if existing.locked_until is not None and existing.locked_until > now:
        await session.commit()
        raise IdempotencyInProgress

    existing.reservation_token = reservation_token
    existing.locked_until = locked_until
    existing.updated_at = now
    await session.commit()
    return _Reservation(
        record_id=existing.record_id,
        reservation_token=reservation_token,
        key_digest=key_digest,
    )


async def _discard_reservation(
    session: AsyncSession, reservation: _Reservation
) -> None:
    await session.rollback()
    if reservation.reservation_token is None:
        return
    await session.execute(
        delete(OperatorEscalationIdempotency).where(
            OperatorEscalationIdempotency.record_id == reservation.record_id,
            OperatorEscalationIdempotency.state == "in_progress",
            OperatorEscalationIdempotency.reservation_token
            == reservation.reservation_token,
        )
    )
    await session.commit()


def _is_active_unique_violation(exc: IntegrityError) -> bool:
    diagnostic = getattr(getattr(exc, "orig", None), "diag", None)
    return (
        getattr(diagnostic, "constraint_name", None)
        == _ACTIVE_UNIQUE_CONSTRAINT
    )


async def create_operator_escalation(
    session: AsyncSession,
    *,
    conversation_id: uuid.UUID,
    reason: str,
    escalation_type: str,
    priority: str,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    idempotency_key: uuid.UUID,
    idempotency_secret: str,
    request_id: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OperatorEscalationResult:
    key_digest, request_fingerprint = _digests(
        secret=idempotency_secret,
        idempotency_key=idempotency_key,
        conversation_id=conversation_id,
        reason=reason,
        escalation_type=escalation_type,
        priority=priority,
    )
    try:
        reservation = await _reserve(
            session,
            actor_account_id=actor_account_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
    except (IdempotencyConflict, IdempotencyInProgress):
        raise
    except OperatorEscalationUnavailable:
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OperatorEscalationUnavailable(
            "idempotency persistence is unavailable"
        ) from exc

    if reservation.replayed:
        try:
            ticket = await session.get(
                EscalationTicket, reservation.replay_ticket_id
            )
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
            await session.rollback()
            raise OperatorEscalationUnavailable(
                "committed escalation result is unavailable"
            ) from exc
        if ticket is None or ticket.created_by_account_id != actor_account_id:
            raise OperatorEscalationUnavailable(
                "committed escalation result is invalid"
            )
        return OperatorEscalationResult(
            ticket=ticket,
            actor_display_name=actor_display_name,
            replayed=True,
        )

    try:
        conversation = await session.get(Conversation, conversation_id)
        if conversation is None:
            await _discard_reservation(session, reservation)
            raise ConversationNotFound

        active_ticket_id = await session.scalar(
            select(EscalationTicket.ticket_id).where(
                EscalationTicket.conversation_id == conversation_id,
                EscalationTicket.status.in_(_ACTIVE_STATUSES),
            )
        )
        if active_ticket_id is not None:
            await _discard_reservation(session, reservation)
            raise EscalationAlreadyOpen

        now = _utcnow()
        ticket = EscalationTicket(
            conversation_id=conversation_id,
            customer_id=conversation.customer_id,
            priority=priority,
            reason=_LEGACY_REASON_BY_TYPE[escalation_type],
            source=_SOURCE,
            escalation_type=escalation_type,
            operator_reason=reason,
            created_by_account_id=actor_account_id,
            status="open",
            transcript_snapshot=[],
            created_at=now,
        )
        session.add(ticket)
        await session.flush()

        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action="operator_escalation_created",
            target_type="escalation_ticket",
            target_id=str(ticket.ticket_id),
            reason_code=escalation_type,
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "conversation_id": str(conversation_id),
                "escalation_type": escalation_type,
                "priority": priority,
                "source": _SOURCE,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=now,
        )

        completed = await session.execute(
            update(OperatorEscalationIdempotency)
            .where(
                OperatorEscalationIdempotency.record_id
                == reservation.record_id,
                OperatorEscalationIdempotency.state == "in_progress",
                OperatorEscalationIdempotency.reservation_token
                == reservation.reservation_token,
            )
            .values(
                state="completed",
                reservation_token=None,
                locked_until=None,
                ticket_id=ticket.ticket_id,
                response_status_code=201,
                updated_at=now,
                completed_at=now,
            )
        )
        if completed.rowcount != 1:
            raise OperatorEscalationUnavailable(
                "idempotency reservation ownership was lost"
            )
        await session.commit()
        return OperatorEscalationResult(
            ticket=ticket,
            actor_display_name=actor_display_name,
            replayed=False,
        )
    except (ConversationNotFound, EscalationAlreadyOpen):
        raise
    except IntegrityError as exc:
        try:
            await _discard_reservation(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as cleanup:
            raise OperatorEscalationUnavailable(
                "escalation rollback is unavailable"
            ) from cleanup
        if _is_active_unique_violation(exc):
            raise EscalationAlreadyOpen from exc
        raise OperatorEscalationUnavailable(
            "escalation persistence is unavailable"
        ) from exc
    except OperatorEscalationUnavailable:
        try:
            await _discard_reservation(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError):
            pass
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        try:
            await _discard_reservation(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError):
            pass
        raise OperatorEscalationUnavailable(
            "escalation persistence is unavailable"
        ) from exc
    except Exception:
        try:
            await _discard_reservation(session, reservation)
        except (SQLAlchemyError, OSError, ConnectionError, TimeoutError):
            pass
        raise
