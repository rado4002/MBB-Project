"""Guarded customer-receipt confirmation from delivering to delivered."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.order_delivery_confirmation import OrderDeliveryConfirmation
from app.models.stock_movement import StockMovement
from app.modules.m4_conversation.order_delivery_evidence import (
    CustomerReceiptEvidenceAmbiguous,
    CustomerReceiptEvidencePreview,
    CustomerReceiptEvidenceInvalid,
    CustomerReceiptEvidenceMissing,
    list_customer_receipt_evidence_candidates,
    read_customer_receipt_evidence,
)
from app.modules.m7_conversion.order_fulfillment import (
    UnsupportedOrderPreparation,
    _single_line,
)
from app.operator_identity.audit import append_operator_audit_event

_AUDIT_ACTION = "order.delivered"


class OrderDeliveryError(Exception):
    """Base error for stable Order delivery confirmation failures."""


class OrderDeliveryForbidden(OrderDeliveryError):
    pass


class OrderDeliveryNotFound(OrderDeliveryError):
    pass


class OrderDeliveryStale(OrderDeliveryError):
    pass


class OrderDeliveryStateConflict(OrderDeliveryError):
    pass


class OrderDeliveryMethodUnsupported(OrderDeliveryError):
    pass


class OrderDeliveryDispatchEvidenceRequired(OrderDeliveryError):
    pass


class OrderDeliveryEvidenceMissing(OrderDeliveryError):
    pass


class OrderDeliveryEvidenceInvalid(OrderDeliveryError):
    pass


class OrderDeliveryEvidenceAmbiguous(OrderDeliveryError):
    pass


class OrderDeliveryEvidenceReused(OrderDeliveryError):
    pass


class OrderDeliveryIdempotencyConflict(OrderDeliveryError):
    pass


class UnsupportedOrderDelivery(OrderDeliveryError):
    pass


class OrderDeliveryUnavailable(OrderDeliveryError):
    pass


@dataclass(frozen=True)
class OrderDeliveryResult:
    order: Order
    confirmation: OrderDeliveryConfirmation
    replayed: bool


@dataclass(frozen=True)
class OrderDeliveryEligibility:
    eligible: bool
    order_state: str
    delivery_method_state: str
    line_state: str
    dispatch_state: str
    association_state: str
    evidence_state: str
    dispatch_occurred_at: datetime | None
    evidence_messages: tuple[CustomerReceiptEvidencePreview, ...]
    evidence_has_more: bool


async def get_order_delivery_eligibility(
    session: AsyncSession,
    *,
    order: Order,
) -> OrderDeliveryEligibility:
    """Project delivery readiness; confirmation rechecks all facts under locks."""
    order_state = (
        "delivering"
        if order.status == "delivering"
        else "delivered"
        if order.status == "delivered"
        else "not_delivering"
    )
    method_state = (
        "supported" if order.delivery_method == "moto_taxi" else "unsupported"
    )
    try:
        sellable_item_id, quantity = _single_line(order)
        line_state = "single"
    except UnsupportedOrderPreparation:
        sellable_item_id, quantity = None, None
        line_state = "unsupported"
    movement = await session.scalar(
        select(StockMovement).where(
            StockMovement.order_id == order.order_id,
            StockMovement.operation_kind == "dispatch",
        )
    )
    dispatch_state = (
        "matched"
        if movement is not None
        and line_state == "single"
        and movement.sellable_item_id == sellable_item_id
        and movement.dispatched_quantity == quantity
        else "missing"
    )
    if (
        order_state != "delivering"
        or method_state != "supported"
        or line_state != "single"
        or dispatch_state != "matched"
    ):
        return OrderDeliveryEligibility(
            eligible=False,
            order_state=order_state,
            delivery_method_state=method_state,
            line_state=line_state,
            dispatch_state=dispatch_state,
            association_state="not_applicable",
            evidence_state="not_applicable",
            dispatch_occurred_at=movement.occurred_at if movement is not None else None,
            evidence_messages=(),
            evidence_has_more=False,
        )
    candidates = await list_customer_receipt_evidence_candidates(
        session,
        order_id=order.order_id,
        customer_id=order.customer_id,
        dispatched_at=movement.occurred_at,
    )
    return OrderDeliveryEligibility(
        eligible=(
            candidates.association_state == "verified"
            and candidates.evidence_state == "available"
        ),
        order_state=order_state,
        delivery_method_state=method_state,
        line_state=line_state,
        dispatch_state=dispatch_state,
        association_state=candidates.association_state,
        evidence_state=candidates.evidence_state,
        dispatch_occurred_at=movement.occurred_at,
        evidence_messages=candidates.items,
        evidence_has_more=candidates.has_more,
    )


def _digest(purpose: bytes, value: bytes) -> str:
    return hashlib.sha256(purpose + value).hexdigest()


def _request_evidence(
    *,
    idempotency_key: uuid.UUID,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    evidence_message_id: uuid.UUID,
) -> tuple[str, str]:
    key_digest = _digest(
        b"order-delivery-key:v1:", str(idempotency_key).encode("ascii")
    )
    canonical = json.dumps(
        {
            "attested": True,
            "evidence_message_id": str(evidence_message_id),
            "expected_updated_at": expected_updated_at.isoformat(),
            "order_id": str(order_id),
            "transition": "delivering_to_delivered",
        },
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return key_digest, _digest(b"order-delivery-request:v1:", canonical)


def _message_fingerprint(message) -> str:
    canonical = json.dumps(
        {
            "content": message.content,
            "content_type": message.content_type,
            "conversation_id": str(message.conversation_id),
            "created_at": message.created_at.isoformat(),
            "direction": message.direction,
            "inbound_receipt_order": message.inbound_receipt_order,
            "message_id": str(message.message_id),
            "provider_message_id": message.whatsapp_message_id,
            "timestamp": message.timestamp.isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return _digest(b"order-delivery-message:v1:", canonical)


def _advisory_key(key_digest: str) -> int:
    value = int(key_digest[:16], 16)
    return value if value < 2**63 else value - 2**64


async def confirm_delivered_order(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    expected_updated_at: datetime,
    evidence_message_id: uuid.UUID,
    attested: bool,
    idempotency_key: uuid.UUID,
    actor_account_id: uuid.UUID,
    actor_display_name: str,
    actor_role: str,
    request_id: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
) -> OrderDeliveryResult:
    """Atomically bind customer evidence and mark one dispatched Order delivered."""
    if actor_role != "administrator" or not isinstance(actor_account_id, uuid.UUID):
        raise OrderDeliveryForbidden("Order delivery confirmation is not permitted")
    if attested is not True:
        raise OrderDeliveryForbidden(
            "explicit customer receipt attestation is required"
        )
    if expected_updated_at.tzinfo is None:
        raise ValueError("expected_updated_at must include a timezone")
    if not isinstance(idempotency_key, uuid.UUID):
        raise ValueError("idempotency_key must be a UUID")
    if not isinstance(evidence_message_id, uuid.UUID):
        raise ValueError("evidence_message_id must be a UUID")
    if not actor_display_name or len(actor_display_name) > 100:
        raise ValueError("actor_display_name is invalid")

    key_digest, request_fingerprint = _request_evidence(
        idempotency_key=idempotency_key,
        order_id=order_id,
        expected_updated_at=expected_updated_at,
        evidence_message_id=evidence_message_id,
    )
    try:
        await session.execute(
            select(func.pg_advisory_xact_lock(_advisory_key(key_digest)))
        )
        prior = await session.scalar(
            select(OrderDeliveryConfirmation).where(
                OrderDeliveryConfirmation.operation_key_digest == key_digest
            )
        )
        if prior is not None:
            if prior.order_id != order_id or not hmac.compare_digest(
                prior.request_fingerprint, request_fingerprint
            ):
                raise OrderDeliveryIdempotencyConflict(
                    "idempotency key was already used for another confirmation"
                )
            order = await session.get(Order, order_id)
            if (
                order is None
                or order.status != "delivered"
                or order.delivered_at != prior.evidence_received_at
            ):
                raise OrderDeliveryUnavailable(
                    "committed delivery confirmation result is invalid"
                )
            await session.commit()
            return OrderDeliveryResult(order=order, confirmation=prior, replayed=True)

        order = await session.scalar(
            select(Order)
            .where(Order.order_id == order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if order is None:
            raise OrderDeliveryNotFound("Order not found")
        if order.status != "delivering" or order.delivered_at is not None:
            raise OrderDeliveryStateConflict(
                "Order is not awaiting delivery confirmation"
            )
        if order.updated_at != expected_updated_at:
            raise OrderDeliveryStale("Order changed since it was reviewed")
        if order.delivery_method != "moto_taxi":
            raise OrderDeliveryMethodUnsupported(
                "only moto_taxi Orders can use this delivery workflow"
            )
        try:
            sellable_item_id, quantity = _single_line(order)
        except UnsupportedOrderPreparation as exc:
            raise UnsupportedOrderDelivery(str(exc)) from exc

        movement = await session.scalar(
            select(StockMovement).where(
                StockMovement.order_id == order_id,
                StockMovement.operation_kind == "dispatch",
            )
        )
        if (
            movement is None
            or movement.sellable_item_id != sellable_item_id
            or movement.dispatched_quantity != quantity
        ):
            raise OrderDeliveryDispatchEvidenceRequired(
                "matching immutable dispatch evidence is required"
            )

        try:
            evidence = await read_customer_receipt_evidence(
                session,
                order_id=order_id,
                customer_id=order.customer_id,
                evidence_message_id=evidence_message_id,
                dispatched_at=movement.occurred_at,
            )
        except CustomerReceiptEvidenceMissing as exc:
            raise OrderDeliveryEvidenceMissing(str(exc)) from exc
        except CustomerReceiptEvidenceInvalid as exc:
            raise OrderDeliveryEvidenceInvalid(str(exc)) from exc
        except CustomerReceiptEvidenceAmbiguous as exc:
            raise OrderDeliveryEvidenceAmbiguous(str(exc)) from exc

        reused = await session.scalar(
            select(OrderDeliveryConfirmation.confirmation_id).where(
                OrderDeliveryConfirmation.evidence_message_id
                == evidence.message.message_id
            )
        )
        if reused is not None:
            raise OrderDeliveryEvidenceReused(
                "customer receipt evidence was already used for another Order"
            )

        confirmed_at = await session.scalar(
            select(
                func.greatest(
                    func.clock_timestamp(),
                    order.updated_at + timedelta(microseconds=1),
                    evidence.message.created_at,
                )
            )
        )
        if confirmed_at is None:
            raise OrderDeliveryUnavailable(
                "Order delivery confirmation timestamp is unavailable"
            )
        confirmation = OrderDeliveryConfirmation(
            order_id=order_id,
            evidence_message_id=evidence.message.message_id,
            evidence_conversation_id=evidence.conversation_id,
            dispatch_movement_id=movement.movement_id,
            evidence_inbound_receipt_order=evidence.message.inbound_receipt_order,
            evidence_received_at=evidence.message.created_at,
            dispatch_occurred_at=movement.occurred_at,
            evidence_fingerprint=_message_fingerprint(evidence.message),
            actor_account_id=actor_account_id,
            operation_key_digest=key_digest,
            request_fingerprint=request_fingerprint,
            confirmed_at=confirmed_at,
        )
        session.add(confirmation)
        order.status = "delivered"
        order.delivered_at = evidence.message.created_at
        order.updated_at = confirmed_at
        await session.flush()
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
                "previous_status": "delivering",
                "new_status": "delivered",
                "expected_updated_at": expected_updated_at.isoformat(),
                "request_fingerprint": request_fingerprint,
                "confirmation_id": str(confirmation.confirmation_id),
                "dispatch_movement_id": str(movement.movement_id),
                "dispatch_occurred_at": movement.occurred_at.isoformat(),
                "evidence_message_id": str(evidence.message.message_id),
                "evidence_conversation_id": str(evidence.conversation_id),
                "evidence_inbound_receipt_order": (
                    evidence.message.inbound_receipt_order
                ),
                "evidence_received_at": evidence.message.created_at.isoformat(),
                "evidence_fingerprint": confirmation.evidence_fingerprint,
                "delivered_at": evidence.message.created_at.isoformat(),
                "attested": True,
            },
            source_network_fingerprint=source_network_fingerprint,
            user_agent_fingerprint=user_agent_fingerprint,
            occurred_at=confirmed_at,
        )
        await session.commit()
        await session.refresh(order)
        await session.refresh(confirmation)
        return OrderDeliveryResult(
            order=order, confirmation=confirmation, replayed=False
        )
    except OrderDeliveryError:
        await session.rollback()
        raise
    except IntegrityError as exc:
        await session.rollback()
        raise OrderDeliveryUnavailable(
            "Order delivery confirmation conflicted with committed evidence"
        ) from exc
    except (SQLAlchemyError, OSError, ConnectionError, TimeoutError) as exc:
        await session.rollback()
        raise OrderDeliveryUnavailable(
            "Order delivery confirmation persistence is unavailable"
        ) from exc
    except Exception as exc:
        await session.rollback()
        raise OrderDeliveryUnavailable(
            "Order delivery confirmation could not be committed"
        ) from exc
