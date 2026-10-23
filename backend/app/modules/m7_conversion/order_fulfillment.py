"""Guarded transition from confirmed Order to preparing fulfillment."""

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
from app.models.stock_allocation import StockAllocation
from app.operator_identity.audit import append_operator_audit_event

_AUDIT_ACTION = "order.prepared"


class OrderPreparationError(Exception):
    """Base error for stable Order preparation failures."""


class OrderPreparationForbidden(OrderPreparationError):
    pass


class OrderPreparationNotFound(OrderPreparationError):
    pass


class OrderPreparationStale(OrderPreparationError):
    pass


class OrderPreparationStateConflict(OrderPreparationError):
    pass


class OrderPreparationPaymentRequired(OrderPreparationError):
    pass


class OrderPreparationAllocationRequired(OrderPreparationError):
    pass


class OrderPreparationIdempotencyConflict(OrderPreparationError):
    pass


class UnsupportedOrderPreparation(OrderPreparationError):
    pass


class OrderPreparationUnavailable(OrderPreparationError):
    pass


@dataclass(frozen=True)
class OrderPreparationResult:
    order: Order
    replayed: bool


def _digest(purpose: bytes, value: bytes) -> str:
    return hashlib.sha256(purpose + value).hexdigest()


def _evidence(
    *,
    idempotency_key: uuid.UUID,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
) -> tuple[str, str]:
    key_digest = _digest(
        b"order-preparation-key:v1:", str(idempotency_key).encode("ascii")
    )
    canonical = json.dumps(
        {
            "expected_updated_at": expected_updated_at.isoformat(),
            "order_id": str(order_id),
            "transition": "confirmed_to_preparing",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return key_digest, _digest(b"order-preparation-request:v1:", canonical)


def _advisory_key(key_digest: str) -> int:
    value = int(key_digest[:16], 16)
    return value if value < 2**63 else value - 2**64


def _single_line(order: Order) -> tuple[uuid.UUID, int]:
    if not isinstance(order.items, list) or len(order.items) != 1:
        raise UnsupportedOrderPreparation(
            "Order preparation currently supports exactly one Order line"
        )
    line = order.items[0]
    if not isinstance(line, dict):
        raise UnsupportedOrderPreparation("Order line is invalid")
    quantity = line.get("quantity")
    if type(quantity) is not int or quantity <= 0:
        raise UnsupportedOrderPreparation("Order line quantity is invalid")
    try:
        sellable_item_id = uuid.UUID(str(line["sellable_item_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise UnsupportedOrderPreparation(
            "Order line has no valid Sellable Item identity"
        ) from exc
    return sellable_item_id, quantity


async def prepare_confirmed_order(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    idempotency_key: uuid.UUID,
    actor_kind: str,
    actor_display_name: str,
    request_id: str | None = None,
    actor_account_id: uuid.UUID | None = None,
    actor_role: str | None = None,
) -> OrderPreparationResult:
    """Atomically prepare one fully paid and allocated confirmed Order."""
    if actor_kind == "human":
        if actor_role != "administrator" or actor_account_id is None:
            raise OrderPreparationForbidden("Order preparation is not permitted")
    elif actor_kind != "service":
        raise OrderPreparationForbidden("Order preparation is not permitted")
    if expected_updated_at.tzinfo is None:
        raise ValueError("expected_updated_at must include a timezone")
    if not isinstance(idempotency_key, uuid.UUID):
        raise ValueError("idempotency_key must be a UUID")
    if not actor_display_name or len(actor_display_name) > 100:
        raise ValueError("actor_display_name is invalid")

    key_digest, request_fingerprint = _evidence(
        idempotency_key=idempotency_key,
        order_id=order_id,
        expected_updated_at=expected_updated_at,
    )
    try:
        await session.execute(
            select(func.pg_advisory_xact_lock(_advisory_key(key_digest)))
        )
        prior = await session.scalar(
            select(OperatorAuditEvent)
            .where(
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
                raise OrderPreparationIdempotencyConflict(
                    "idempotency key was already used for another request"
                )
            order = await session.get(Order, order_id)
            if order is None or order.status != "preparing":
                raise OrderPreparationUnavailable(
                    "committed preparation result is invalid"
                )
            await session.commit()
            return OrderPreparationResult(order=order, replayed=True)

        order = await session.scalar(
            select(Order)
            .where(Order.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise OrderPreparationNotFound("Order not found")
        if order.status != "confirmed":
            raise OrderPreparationStateConflict(
                "Order is not eligible to begin preparation"
            )
        if order.updated_at != expected_updated_at:
            raise OrderPreparationStale("Order changed since it was reviewed")
        sellable_item_id, quantity = _single_line(order)

        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id).with_for_update()
        )
        if (
            payment is None
            or payment.status != "completed"
            or payment.completed_at is None
            or payment.amount != order.total_amount
            or payment.currency != order.currency
        ):
            raise OrderPreparationPaymentRequired(
                "matching verified completed Payment is required"
            )

        allocation = await session.scalar(
            select(StockAllocation)
            .where(StockAllocation.order_id == order_id)
            .with_for_update()
        )
        if (
            allocation is None
            or allocation.sellable_item_id != sellable_item_id
            or allocation.quantity != quantity
        ):
            raise OrderPreparationAllocationRequired(
                "full durable Stock allocation is required"
            )

        occurred_at = await session.scalar(
            select(
                func.greatest(
                    func.clock_timestamp(),
                    order.updated_at + timedelta(microseconds=1),
                )
            )
        )
        if occurred_at is None:
            raise OrderPreparationUnavailable(
                "Order preparation timestamp is unavailable"
            )
        order.status = "preparing"
        order.updated_at = occurred_at
        await append_operator_audit_event(
            session,
            category="business",
            actor_kind=actor_kind,
            actor_account_id=actor_account_id,
            actor_display_name=actor_display_name,
            effective_role=actor_role if actor_kind == "human" else None,
            request_id=request_id,
            action=_AUDIT_ACTION,
            target_type="order",
            target_id=str(order_id),
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "previous_status": "confirmed",
                "new_status": "preparing",
                "expected_updated_at": expected_updated_at.isoformat(),
                "request_fingerprint": request_fingerprint,
                "payment_id": str(payment.payment_id),
                "allocation_quantity": allocation.quantity,
                "sellable_item_id": str(allocation.sellable_item_id),
            },
            occurred_at=occurred_at,
        )
        await session.commit()
        await session.refresh(order)
        return OrderPreparationResult(order=order, replayed=False)
    except OrderPreparationError:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OrderPreparationUnavailable(
            "Order preparation persistence is unavailable"
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise OrderPreparationUnavailable(
            "Order preparation could not be committed"
        ) from exc
