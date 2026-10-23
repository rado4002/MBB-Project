"""Administrator cancellation of payment-free pending Orders."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operator_audit import OperatorAuditEvent
from app.models.order import Order
from app.models.payment import Payment
from app.operator_identity.audit import append_operator_audit_event

ORDER_CANCELLATION_REASONS = frozenset(
    {
        "customer_requested",
        "duplicate_order",
        "commercial_terms_unavailable",
        "other_operational",
    }
)
_AUDIT_ACTION = "order.cancelled"


class OrderCancellationError(Exception):
    """Base class for stable Order cancellation failures."""


class OrderCancellationForbidden(OrderCancellationError):
    pass


class OrderNotFound(OrderCancellationError):
    pass


class OrderCancellationStale(OrderCancellationError):
    pass


class OrderCancellationStateConflict(OrderCancellationError):
    pass


class OrderCancellationPaymentConflict(OrderCancellationError):
    pass


class OrderCancellationIdempotencyConflict(OrderCancellationError):
    pass


class OrderCancellationUnavailable(OrderCancellationError):
    pass


@dataclass(frozen=True)
class OrderCancellationResult:
    order: Order
    replayed: bool


def _digest(secret: str, purpose: bytes, value: bytes) -> str:
    encoded = secret.encode("utf-8")
    if len(encoded) < 32:
        raise OrderCancellationUnavailable(
            "browser idempotency protection is unavailable"
        )
    return hmac.new(encoded, purpose + value, hashlib.sha256).hexdigest()


def _digests(
    *,
    secret: str,
    idempotency_key: uuid.UUID,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    reason_code: str,
) -> tuple[str, str]:
    key_digest = _digest(
        secret,
        b"order-cancellation-key:v1:",
        str(idempotency_key).encode("ascii"),
    )
    canonical = json.dumps(
        {
            "expected_updated_at": expected_updated_at.isoformat(),
            "order_id": str(order_id),
            "reason_code": reason_code,
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request_fingerprint = _digest(
        secret,
        b"order-cancellation-request:v1:",
        canonical,
    )
    return key_digest, request_fingerprint


def _advisory_key(key_digest: str) -> int:
    value = int(key_digest[:16], 16)
    return value if value < 2**63 else value - 2**64


async def cancel_pending_order(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    reason_code: str,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    idempotency_key: uuid.UUID,
    idempotency_secret: str,
    request_id: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OrderCancellationResult:
    """Cancel one pending Order when no Payment has ever been attached.

    The transaction-scoped advisory lock serializes use of an idempotency key,
    including concurrent attempts aimed at different Orders. The Order row lock
    serializes different cancellation keys for the same Order. Locking the Order
    also conflicts with the Payment table's status guard, which checks the
    Order after acquiring a row lock. Any existing Payment fails closed.
    """
    if actor_role != "administrator":
        raise OrderCancellationForbidden
    if reason_code not in ORDER_CANCELLATION_REASONS:
        raise ValueError("unsupported cancellation reason")
    if expected_updated_at.tzinfo is None:
        raise ValueError("expected_updated_at must include a timezone")

    key_digest, request_fingerprint = _digests(
        secret=idempotency_secret,
        idempotency_key=idempotency_key,
        order_id=order_id,
        expected_updated_at=expected_updated_at,
        reason_code=reason_code,
    )
    try:
        await session.execute(
            select(func.pg_advisory_xact_lock(_advisory_key(key_digest)))
        )
        prior = await session.scalar(
            select(OperatorAuditEvent)
            .where(
                OperatorAuditEvent.actor_account_id == actor_account_id,
                OperatorAuditEvent.action == _AUDIT_ACTION,
                OperatorAuditEvent.idempotency_reference == key_digest,
            )
            .order_by(OperatorAuditEvent.occurred_at)
            .limit(1)
        )
        if prior is not None:
            prior_fingerprint = str(
                (prior.event_metadata or {}).get("request_fingerprint", "")
            )
            if prior.target_id != str(order_id) or not hmac.compare_digest(
                prior_fingerprint, request_fingerprint
            ):
                raise OrderCancellationIdempotencyConflict
            order = await session.get(Order, order_id)
            if order is None or order.status != "cancelled":
                raise OrderCancellationUnavailable(
                    "committed cancellation result is invalid"
                )
            await session.commit()
            return OrderCancellationResult(order=order, replayed=True)

        order = await session.scalar(
            select(Order)
            .where(Order.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise OrderNotFound
        if order.status != "pending":
            raise OrderCancellationStateConflict
        if order.updated_at != expected_updated_at:
            raise OrderCancellationStale

        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id).with_for_update()
        )
        # Pending and failed Payments may still settle later. Refunded and
        # completed Payments also need Payment-owned handling. All fail closed.
        if payment is not None:
            raise OrderCancellationPaymentConflict

        now = await session.scalar(
            select(
                func.greatest(
                    func.clock_timestamp(),
                    order.updated_at + timedelta(microseconds=1),
                )
            )
        )
        if now is None:
            raise OrderCancellationUnavailable(
                "Order cancellation timestamp is unavailable"
            )
        order.status = "cancelled"
        order.updated_at = now
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind="human",
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role,
            request_id=request_id,
            action=_AUDIT_ACTION,
            target_type="order",
            target_id=str(order_id),
            reason_code=reason_code,
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "previous_status": "pending",
                "new_status": "cancelled",
                "expected_updated_at": expected_updated_at.isoformat(),
                "request_fingerprint": request_fingerprint,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=now,
        )
        await session.commit()
        await session.refresh(order)
        return OrderCancellationResult(order=order, replayed=False)
    except OrderCancellationError:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OrderCancellationUnavailable(
            "Order cancellation persistence is unavailable"
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise OrderCancellationUnavailable(
            "Order cancellation could not be committed"
        ) from exc
