"""Provider-independent creation of one pending Payment intent per Order."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.schemas.common import PaymentMethod


class PaymentIntentError(Exception):
    """Base error for stable Payment intent failures."""


class PaymentIntentNotFound(PaymentIntentError):
    pass


class PaymentIntentStateConflict(PaymentIntentError):
    pass


class PaymentIntentAcceptanceRequired(PaymentIntentError):
    pass


class UnsupportedPaymentIntent(PaymentIntentError):
    pass


class PaymentIntentIdempotencyConflict(PaymentIntentError):
    pass


class PaymentIntentConflict(PaymentIntentError):
    pass


@dataclass(frozen=True)
class PaymentIntentResult:
    payment: Payment
    customer_id: str
    replayed: bool
    verification_state: Literal["unverified"] = "unverified"


def _digest(purpose: bytes, value: bytes) -> str:
    return hashlib.sha256(purpose + value).hexdigest()


def _evidence(
    *, idempotency_key: uuid.UUID, order_id: uuid.UUID, method: PaymentMethod
) -> tuple[str, str]:
    key_digest = _digest(
        b"payment-intent-key:v1:", str(idempotency_key).encode("ascii")
    )
    canonical = json.dumps(
        {"method": method.value, "order_id": str(order_id)},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return key_digest, _digest(b"payment-intent-request:v1:", canonical)


def _advisory_key(key_digest: str) -> int:
    value = int(key_digest[:16], 16)
    return value if value < 2**63 else value - 2**64


def _single_line(order: Order) -> tuple[uuid.UUID, int, Decimal]:
    if not isinstance(order.items, list) or len(order.items) != 1:
        raise UnsupportedPaymentIntent(
            "Payment intents currently support exactly one Order line"
        )
    line = order.items[0]
    if not isinstance(line, dict):
        raise UnsupportedPaymentIntent("Order line is invalid")
    quantity = line.get("quantity")
    if type(quantity) is not int or quantity <= 0:
        raise UnsupportedPaymentIntent("Order line quantity is invalid")
    try:
        sellable_item_id = uuid.UUID(str(line["sellable_item_id"]))
        unit_price = Decimal(str(line["unit_price_cdf"]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise UnsupportedPaymentIntent("Order line snapshot is incomplete") from exc
    if unit_price <= 0 or unit_price * quantity != order.total_amount:
        raise UnsupportedPaymentIntent("Order amount does not match its accepted line")
    return sellable_item_id, quantity, unit_price


def _matches(
    payment: Payment,
    *,
    order: Order,
    method: PaymentMethod,
    request_fingerprint: str,
) -> bool:
    return (
        payment.intent_request_fingerprint == request_fingerprint
        and payment.method == method.value
        and payment.amount == order.total_amount
        and payment.currency == order.currency
        and payment.customer_id == order.customer_id
    )


async def create_payment_intent(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    method: PaymentMethod,
    idempotency_key: uuid.UUID,
) -> PaymentIntentResult:
    """Create one unverified pending Payment from an accepted Order snapshot.

    The caller controls commit or rollback. This function performs no provider
    call, settlement claim, Order transition, Stock allocation, or external send.
    """
    if not isinstance(method, PaymentMethod):
        raise UnsupportedPaymentIntent(
            "an explicit supported Payment method is required"
        )
    if not isinstance(idempotency_key, uuid.UUID):
        raise ValueError("idempotency_key must be a UUID")

    key_digest, request_fingerprint = _evidence(
        idempotency_key=idempotency_key, order_id=order_id, method=method
    )
    await session.execute(
        select(func.pg_advisory_xact_lock(_advisory_key(key_digest)))
    )
    order = await session.scalar(
        select(Order)
        .where(Order.order_id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if order is None:
        raise PaymentIntentNotFound("Order was not found")
    sellable_item_id, quantity, _unit_price = _single_line(order)

    keyed = await session.scalar(
        select(Payment)
        .where(Payment.intent_idempotency_key_digest == key_digest)
        .with_for_update()
    )
    if keyed is not None and (
        keyed.order_id != order_id
        or keyed.intent_request_fingerprint != request_fingerprint
    ):
        raise PaymentIntentIdempotencyConflict(
            "the idempotency key was already used for another Payment intent"
        )

    existing = await session.scalar(
        select(Payment).where(Payment.order_id == order_id).with_for_update()
    )
    if existing is not None:
        if existing.intent_idempotency_key_digest is None:
            raise PaymentIntentConflict(
                "Order already has Payment state outside the intent contract"
            )
        if not _matches(
            existing,
            order=order,
            method=method,
            request_fingerprint=request_fingerprint,
        ):
            raise PaymentIntentConflict(
                "Order already has a different Payment intent"
            )
        return PaymentIntentResult(
            payment=existing, customer_id=order.customer_id, replayed=True
        )

    if order.status != "pending":
        raise PaymentIntentStateConflict(
            "Order must be pending before Payment intent creation"
        )
    draft = await session.scalar(
        select(OrderDraft)
        .where(
            OrderDraft.order_id == order_id,
            OrderDraft.status == "confirmed",
        )
        .with_for_update()
    )
    if (
        draft is None
        or draft.sellable_item_id != sellable_item_id
        or draft.quantity != quantity
        or draft.total_cdf != order.total_amount
    ):
        raise PaymentIntentAcceptanceRequired(
            "Order has no matching accepted snapshot"
        )

    payment = Payment(
        order_id=order_id,
        customer_id=order.customer_id,
        intent_idempotency_key_digest=key_digest,
        intent_request_fingerprint=request_fingerprint,
        method=method.value,
        amount=order.total_amount,
        currency=order.currency,
        provider_transaction_id=None,
        provider_response=None,
        status="pending",
        failure_reason=None,
        completed_at=None,
    )
    session.add(payment)
    await session.flush()
    return PaymentIntentResult(
        payment=payment, customer_id=order.customer_id, replayed=False
    )
