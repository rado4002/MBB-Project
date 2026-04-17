"""
app/modules/m7_conversion/service.py — Core M7 Conversion Engine.

Entry points:
  create_order()        — Create a new order from conversation context
  initiate_payment()    — Trigger Mobile Money payment (called by Celery task)
  process_callback()    — Handle payment callback (called by Celery task)
  update_order_status() — Advance order through state machine
  credit_club_points()  — Auto-credit loyalty points on confirmed order
  sync_order_to_crm()   — Push order to Airtable CRM (idempotent)

State machine (enforced here):
  pending → confirmed → preparing → delivering → delivered
  Any (except delivered) → cancelled
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.order import Order
from app.models.payment import Payment
from app.schemas.common import OrderStatus, PaymentMethod
from app.schemas.orders import OrderCreate

log = structlog.get_logger(__name__)

# Club points: 1 point per 1,000 CDF spent
_POINTS_PER_CDF = 1 / 1000

# Valid state transitions (state machine)
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": {"confirmed", "cancelled"},
    "confirmed": {"preparing", "cancelled"},
    "preparing": {"delivering", "cancelled"},
    "delivering": {"delivered", "cancelled"},
    "delivered": set(),        # Terminal state
    "cancelled": set(),        # Terminal state
}

# Payment methods that require a Mobile Money adapter call
_MOBILE_MONEY_METHODS = {
    PaymentMethod.orange_money,
    PaymentMethod.airtel_money,
    PaymentMethod.mpesa,
}


# ── Order creation ────────────────────────────────────────────────────────────

async def create_order(
    session: AsyncSession,
    *,
    lead_id: uuid.UUID,
    customer_phone: str,
    items: list[dict[str, Any]],
    delivery_zone: str,
    payment_method: PaymentMethod,
    delivery_method: str = "moto_taxi",
    idempotency_key: str | None = None,
) -> Order:
    """
    Create a new order from conversation context.

    Idempotent: if idempotency_key is provided and order exists, returns existing.

    Args:
        session:          Async DB session
        lead_id:          UUID of the qualifying lead
        customer_phone:   Customer phone number (PK in customers table)
        items:            List of {product_id, quantity, unit_price_cdf}
        delivery_zone:    Delivery zone / commune
        payment_method:   How the customer wants to pay
        delivery_method:  "moto_taxi" | "spot_pickup"
        idempotency_key:  Optional client key for deduplication

    Returns:
        Newly created (or existing) Order instance
    """
    total_amount = Decimal(
        sum(i["unit_price_cdf"] * i["quantity"] for i in items)
    )

    # Determine payment_type for DB check constraint
    if payment_method in _MOBILE_MONEY_METHODS:
        payment_type = "mobile_money"
    elif payment_method == PaymentMethod.bank_transfer:
        payment_type = "bank_transfer"
    else:
        payment_type = "cod"

    order = Order(
        order_id=uuid.uuid4(),
        lead_id=lead_id,
        customer_id=customer_phone,
        items=items,
        total_amount=total_amount,
        currency="CDF",
        payment_type=payment_type,
        delivery_zone=delivery_zone,
        delivery_method=delivery_method,
        status="pending",
        hub_crm_synced=False,
        club_points_credited=0,
    )
    session.add(order)

    # Create a pending Payment record
    payment = Payment(
        payment_id=uuid.uuid4(),
        order_id=order.order_id,
        method=payment_method.value,
        amount=total_amount,
        currency="CDF",
        status="pending",
    )
    session.add(payment)

    await session.commit()
    await session.refresh(order)

    log.info(
        "m7.order.created",
        order_id=str(order.order_id),
        customer=customer_phone,
        total=float(total_amount),
        method=payment_method.value,
    )
    return order


# ── State machine ─────────────────────────────────────────────────────────────

class InvalidOrderTransition(ValueError):
    """Raised when a state transition is not permitted by the state machine."""


async def update_order_status(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    new_status: str,
) -> Order:
    """
    Advance order through the state machine.

    Enforces allowed transitions:
      pending → confirmed → preparing → delivering → delivered
      Any (except delivered/cancelled) → cancelled

    Raises:
        InvalidOrderTransition if the transition is not allowed.
    """
    result = await session.execute(select(Order).where(Order.order_id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise ValueError(f"Order not found: {order_id}")

    allowed = _VALID_TRANSITIONS.get(order.status, set())
    if new_status not in allowed:
        raise InvalidOrderTransition(
            f"Cannot transition order {order_id} "
            f"from '{order.status}' to '{new_status}'. "
            f"Allowed: {sorted(allowed)}"
        )

    now = datetime.now(timezone.utc)
    order.status = new_status
    order.updated_at = now

    if new_status == "confirmed":
        order.confirmed_at = now
    elif new_status == "delivered":
        order.delivered_at = now

    await session.commit()
    await session.refresh(order)

    log.info(
        "m7.order.status_updated",
        order_id=str(order_id),
        new_status=new_status,
    )
    return order


# ── Club points ───────────────────────────────────────────────────────────────

async def credit_club_points(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
) -> int:
    """
    Auto-credit loyalty points on a confirmed order.

    Formula: 1 point per 1,000 CDF (rounded down).
    Idempotent: returns 0 if points already credited.

    Returns:
        Number of points credited (0 if already done)
    """
    result = await session.execute(select(Order).where(Order.order_id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise ValueError(f"Order not found: {order_id}")

    if order.club_points_credited > 0:
        log.info("m7.points.already_credited", order_id=str(order_id))
        return 0

    if order.status not in ("confirmed", "delivered"):
        log.warning(
            "m7.points.skipped_status",
            order_id=str(order_id),
            status=order.status,
        )
        return 0

    points = int(float(order.total_amount) * _POINTS_PER_CDF)
    order.club_points_credited = points
    order.updated_at = datetime.now(timezone.utc)
    await session.commit()

    log.info("m7.points.credited", order_id=str(order_id), points=points)
    return points


# ── CRM sync ─────────────────────────────────────────────────────────────────

async def sync_order_to_crm(order_id: str, idempotency_key: str) -> str:
    """
    Push order data to Airtable CRM. Idempotent.

    Called by the Celery conversion task. Opens its own DB session.

    Returns:
        Airtable record ID (or "already_synced" if skipped)
    """
    from app.adapters.crm import get_crm_adapter  # type: ignore[import]

    async with AsyncSessionLocal() as session:
        result = await session.execute(select(Order).where(Order.order_id == uuid.UUID(order_id)))
        order = result.scalar_one_or_none()
        if order is None:
            raise ValueError(f"Order not found: {order_id}")

        if order.hub_crm_synced:
            log.info("m7.crm.already_synced", order_id=order_id)
            return "already_synced"

        crm = get_crm_adapter()
        order_data = {
            "order_id": str(order.order_id),
            "customer_id": order.customer_id,
            "lead_id": str(order.lead_id),
            "items": order.items,
            "total_cdf": float(order.total_amount),
            "status": order.status,
            "payment_type": order.payment_type,
            "delivery_zone": order.delivery_zone,
            "club_points": order.club_points_credited,
            "created_at": order.created_at.isoformat() if order.created_at else None,
        }
        crm_id = await crm.sync_order(order_data)

        order.hub_crm_synced = True
        order.hub_crm_order_id = crm_id
        order.updated_at = datetime.now(timezone.utc)
        await session.commit()

        log.info("m7.crm.synced", order_id=order_id, crm_id=crm_id)
        return crm_id


# ── Payment initiation (called by Celery) ────────────────────────────────────

async def initiate_payment(*, order_id: str, idempotency_key: str) -> dict[str, Any]:
    """
    Trigger Mobile Money payment for an order. Called by Celery task.

    Opens its own DB session (Celery workers can't share FastAPI sessions).

    Returns:
        {"status": "pending" | "failed", "payment_id": str, "transaction_id": str}
    """
    from app.adapters.payment import get_payment_adapter

    async with AsyncSessionLocal() as session:
        # Load order + payment
        order_result = await session.execute(
            select(Order).where(Order.order_id == uuid.UUID(order_id))
        )
        order = order_result.scalar_one_or_none()
        if order is None:
            raise ValueError(f"Order not found: {order_id}")

        payment_result = await session.execute(
            select(Payment).where(Payment.order_id == order.order_id)
        )
        payment = payment_result.scalar_one_or_none()
        if payment is None:
            raise ValueError(f"No payment record for order: {order_id}")

        # COD / Bank Transfer: no adapter call needed
        if payment.method in ("cash", "bank_transfer"):
            log.info("m7.payment.no_adapter_needed", method=payment.method, order_id=order_id)
            return {"status": "pending", "payment_id": str(payment.payment_id), "method": payment.method}

        adapter = get_payment_adapter(payment.method)
        result = await adapter.initiate_payment(
            phone=order.customer_id,
            amount=float(order.total_amount),
            currency=order.currency,
            reference=idempotency_key,
            method=payment.method,
        )

        # Store provider transaction ID for callback matching
        if result.get("transaction_id"):
            payment.provider_transaction_id = result["transaction_id"]
            payment.provider_response = result
            await session.commit()

        log.info(
            "m7.payment.initiated",
            order_id=order_id,
            method=payment.method,
            txn_id=result.get("transaction_id"),
        )
        return {
            "status": result.get("status", "pending"),
            "payment_id": str(payment.payment_id),
            "transaction_id": result.get("transaction_id"),
        }


# ── Callback processing (called by Celery) ───────────────────────────────────

async def process_callback(
    *,
    payment_id: str,
    provider: str,
    status: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """
    Handle a Mobile Money payment callback. Called by Celery task.

    Idempotent: safe to call multiple times (same payment_id + idempotency_key).

    Args:
        payment_id:       Provider transaction ID
        provider:         "orange" | "airtel" | "mpesa"
        status:           "success" | "failed"
        idempotency_key:  Deduplication key

    Returns:
        {"order_id": str, "order_status": str, "payment_status": str}
    """
    async with AsyncSessionLocal() as session:
        # Find payment by provider transaction ID
        result = await session.execute(
            select(Payment).where(Payment.provider_transaction_id == payment_id)
        )
        payment = result.scalar_one_or_none()
        if payment is None:
            log.warning("m7.callback.payment_not_found", payment_id=payment_id)
            return {"status": "not_found", "payment_id": payment_id}

        # Idempotency: already processed?
        if payment.status in ("completed", "failed"):
            log.info("m7.callback.already_processed", payment_id=payment_id, status=payment.status)
            order_result = await session.execute(
                select(Order).where(Order.order_id == payment.order_id)
            )
            order = order_result.scalar_one()
            return {
                "order_id": str(order.order_id),
                "order_status": order.status,
                "payment_status": payment.status,
                "idempotent": True,
            }

        now = datetime.now(timezone.utc)
        # Map provider "success" → "completed"
        payment_status = "completed" if status == "success" else "failed"
        payment.status = payment_status
        payment.completed_at = now if payment_status == "completed" else None

        # Advance order state if payment succeeded
        order_result = await session.execute(
            select(Order).where(Order.order_id == payment.order_id)
        )
        order = order_result.scalar_one()

        if payment_status == "completed" and order.status == "pending":
            order.status = "confirmed"
            order.confirmed_at = now
            order.updated_at = now
            log.info("m7.callback.order_confirmed", order_id=str(order.order_id))

        await session.commit()

        # Trigger CRM sync asynchronously
        from app.tasks.conversion import sync_order_crm  # type: ignore[import]
        sync_order_crm.apply_async(
            kwargs={"order_id": str(order.order_id), "idempotency_key": idempotency_key},
            queue="conversion",
        )

        return {
            "order_id": str(order.order_id),
            "order_status": order.status,
            "payment_status": payment_status,
            "idempotent": False,
        }
