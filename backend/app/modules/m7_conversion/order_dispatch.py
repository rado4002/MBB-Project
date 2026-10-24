"""Guarded physical dispatch from preparing Order to delivering."""

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

from app.models.inventory import InventoryRecord
from app.models.order import Order
from app.models.payment import Payment
from app.models.stock_allocation import StockAllocation
from app.models.stock_movement import StockMovement
from app.modules.commerce_admin import (
    CommerceAdminContext,
    CommerceAuthorizationDenied,
)
from app.modules.inventory.service import (
    InventoryDispatchConflict,
    consume_order_allocation,
)
from app.modules.m7_conversion.order_fulfillment import (
    UnsupportedOrderPreparation,
    _allocation_state,
    _payment_state,
    _single_line,
)
from app.operator_identity.audit import append_operator_audit_event

_AUDIT_ACTION = "order.dispatched"


class OrderDispatchError(Exception):
    """Base error for stable Order dispatch failures."""


class OrderDispatchForbidden(OrderDispatchError):
    pass


class OrderDispatchNotFound(OrderDispatchError):
    pass


class OrderDispatchStale(OrderDispatchError):
    pass


class OrderDispatchStateConflict(OrderDispatchError):
    pass


class OrderDispatchPaymentRequired(OrderDispatchError):
    pass


class OrderDispatchAllocationRequired(OrderDispatchError):
    pass


class OrderDispatchMethodUnsupported(OrderDispatchError):
    pass


class OrderDispatchIdempotencyConflict(OrderDispatchError):
    pass


class UnsupportedOrderDispatch(OrderDispatchError):
    pass


class OrderDispatchUnavailable(OrderDispatchError):
    pass


@dataclass(frozen=True)
class OrderDispatchResult:
    order: Order
    movement: StockMovement
    replayed: bool


@dataclass(frozen=True)
class OrderDispatchEligibility:
    eligible: bool
    order_state: str
    payment_state: str
    allocation_state: str
    inventory_state: str
    delivery_method_state: str
    required_quantity: int | None


def _digest(purpose: bytes, value: bytes) -> str:
    return hashlib.sha256(purpose + value).hexdigest()


def _evidence(
    *,
    idempotency_key: uuid.UUID,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    handoff_reference: str,
) -> tuple[str, str]:
    key_digest = _digest(
        b"order-dispatch-key:v1:", str(idempotency_key).encode("ascii")
    )
    canonical = json.dumps(
        {
            "attested": True,
            "expected_updated_at": expected_updated_at.isoformat(),
            "handoff_reference": handoff_reference,
            "order_id": str(order_id),
            "transition": "preparing_to_delivering",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return key_digest, _digest(b"order-dispatch-request:v1:", canonical)


def _advisory_key(key_digest: str) -> int:
    value = int(key_digest[:16], 16)
    return value if value < 2**63 else value - 2**64


def _normalize_reference(value: str) -> str:
    normalized = value.strip()
    if not 1 <= len(normalized) <= 100 or any(
        ord(character) < 32 or ord(character) == 127 for character in normalized
    ):
        raise ValueError("handoff_reference is invalid")
    return normalized


async def get_order_dispatch_eligibility(
    session: AsyncSession,
    *,
    order: Order,
) -> OrderDispatchEligibility:
    """Project dispatch readiness; the write path rechecks under locks."""
    order_state = "preparing" if order.status == "preparing" else "not_preparing"
    method_state = (
        "supported" if order.delivery_method == "moto_taxi" else "unsupported"
    )
    payment = await session.scalar(
        select(Payment).where(Payment.order_id == order.order_id)
    )
    payment_state = _payment_state(payment, order)
    try:
        sellable_item_id, quantity = _single_line(order)
    except UnsupportedOrderPreparation:
        return OrderDispatchEligibility(
            eligible=False,
            order_state=order_state,
            payment_state=payment_state,
            allocation_state="unsupported",
            inventory_state="unsupported",
            delivery_method_state=method_state,
            required_quantity=None,
        )
    allocation = await session.scalar(
        select(StockAllocation).where(StockAllocation.order_id == order.order_id)
    )
    movement = await session.scalar(
        select(StockMovement).where(
            StockMovement.operation_kind == "dispatch",
            StockMovement.order_id == order.order_id,
        )
    )
    if movement is not None:
        allocation_state = "consumed"
    else:
        state = _allocation_state(
            allocation,
            sellable_item_id=sellable_item_id,
            quantity=quantity,
        )
        allocation_state = "full_unconsumed" if state == "full" else state
    inventory = await session.scalar(
        select(InventoryRecord).where(
            InventoryRecord.sellable_item_id == sellable_item_id
        )
    )
    inventory_state = (
        "ready"
        if inventory is not None
        and inventory.quantity is not None
        and inventory.quantity >= quantity
        and inventory.allocated_quantity >= quantity
        else "conflict"
    )
    return OrderDispatchEligibility(
        eligible=(
            order_state == "preparing"
            and method_state == "supported"
            and payment_state == "verified"
            and allocation_state == "full_unconsumed"
            and inventory_state == "ready"
        ),
        order_state=order_state,
        payment_state=payment_state,
        allocation_state=allocation_state,
        inventory_state=inventory_state,
        delivery_method_state=method_state,
        required_quantity=quantity,
    )


async def dispatch_prepared_order(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    idempotency_key: uuid.UUID,
    handoff_reference: str,
    attested: bool,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    request_id: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OrderDispatchResult:
    """Atomically consume Stock and dispatch one prepared Order."""
    if actor_role != "administrator" or not isinstance(actor_account_id, uuid.UUID):
        raise OrderDispatchForbidden("Order dispatch is not permitted")
    if attested is not True:
        raise OrderDispatchForbidden("explicit dispatch attestation is required")
    if expected_updated_at.tzinfo is None:
        raise ValueError("expected_updated_at must include a timezone")
    if not isinstance(idempotency_key, uuid.UUID):
        raise ValueError("idempotency_key must be a UUID")
    if not actor_display_name or len(actor_display_name) > 100:
        raise ValueError("actor_display_name is invalid")
    reference = _normalize_reference(handoff_reference)
    key_digest, request_fingerprint = _evidence(
        idempotency_key=idempotency_key,
        order_id=order_id,
        expected_updated_at=expected_updated_at,
        handoff_reference=reference,
    )
    administrator = CommerceAdminContext(
        actor_account_id=actor_account_id,
        request_id=request_id,
        source_network_fingerprint=source_network_fingerprint,
        user_agent_fingerprint=user_agent_fingerprint,
    )
    try:
        await session.execute(
            select(func.pg_advisory_xact_lock(_advisory_key(key_digest)))
        )
        prior = await session.scalar(
            select(StockMovement).where(
                StockMovement.operation_kind == "dispatch",
                StockMovement.operation_key == idempotency_key,
            )
        )
        if prior is not None:
            if prior.order_id != order_id or not hmac.compare_digest(
                prior.dispatch_request_fingerprint or "", request_fingerprint
            ):
                raise OrderDispatchIdempotencyConflict(
                    "idempotency key was already used for another dispatch"
                )
            order = await session.get(Order, order_id)
            if order is None or order.status != "delivering":
                raise OrderDispatchUnavailable("committed dispatch result is invalid")
            await session.commit()
            return OrderDispatchResult(order=order, movement=prior, replayed=True)

        order = await session.scalar(
            select(Order)
            .where(Order.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise OrderDispatchNotFound("Order not found")
        if order.status != "preparing":
            raise OrderDispatchStateConflict("Order is not prepared for dispatch")
        if order.updated_at != expected_updated_at:
            raise OrderDispatchStale("Order changed since it was reviewed")
        if order.delivery_method != "moto_taxi":
            raise OrderDispatchMethodUnsupported(
                "only moto_taxi Orders can use this dispatch workflow"
            )
        try:
            sellable_item_id, quantity = _single_line(order)
        except UnsupportedOrderPreparation as exc:
            raise UnsupportedOrderDispatch(str(exc)) from exc

        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id).with_for_update()
        )
        if _payment_state(payment, order) != "verified":
            raise OrderDispatchPaymentRequired(
                "matching verified completed Payment is required"
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
            raise OrderDispatchUnavailable("Order dispatch timestamp is unavailable")
        try:
            inventory, movement = await consume_order_allocation(
                session,
                order_id=order_id,
                sellable_item_id=sellable_item_id,
                quantity=quantity,
                operation_key=idempotency_key,
                request_fingerprint=request_fingerprint,
                handoff_reference=reference,
                administrator=administrator,
                event_time=occurred_at,
            )
        except CommerceAuthorizationDenied as exc:
            raise OrderDispatchForbidden(str(exc)) from exc
        except InventoryDispatchConflict as exc:
            raise OrderDispatchAllocationRequired(str(exc)) from exc

        order.status = "delivering"
        order.updated_at = movement.occurred_at
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
            outcome="succeeded",
            idempotency_reference=key_digest,
            metadata={
                "previous_status": "preparing",
                "new_status": "delivering",
                "expected_updated_at": expected_updated_at.isoformat(),
                "request_fingerprint": request_fingerprint,
                "payment_id": str(payment.payment_id),
                "movement_id": str(movement.movement_id),
                "sellable_item_id": str(sellable_item_id),
                "dispatched_quantity": quantity,
                "physical_before_quantity": movement.before_quantity,
                "physical_after_quantity": movement.after_quantity,
                "allocated_before_quantity": inventory.allocated_quantity + quantity,
                "allocated_after_quantity": inventory.allocated_quantity,
                "delivery_method": order.delivery_method,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=movement.occurred_at,
        )
        await session.commit()
        await session.refresh(order)
        return OrderDispatchResult(order=order, movement=movement, replayed=False)
    except OrderDispatchError:
        await session.rollback()
        raise
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OrderDispatchUnavailable(
            "Order dispatch persistence is unavailable"
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise OrderDispatchUnavailable("Order dispatch could not be committed") from exc
