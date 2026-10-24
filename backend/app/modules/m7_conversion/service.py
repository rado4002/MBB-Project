"""
app/modules/m7_conversion/service.py — Core M7 Conversion Engine.

Entry points:
  initiate_payment()    — Fail-closed legacy payment task
  process_callback()    — Fail-closed legacy callback task
  update_order_status() — Guard preparation and reject protected lifecycle bypasses
  credit_club_points()  — Auto-credit loyalty points on confirmed order
  sync_order_to_crm()   — Push order to Airtable CRM (idempotent)

The generic path cannot establish verified Payment or Stock allocation. It
rejects pending → confirmed and delegates confirmed → preparing to the guarded
fulfillment contract only when callers supply concurrency and audit evidence.
Cancellation belongs to order_cancellation.cancel_pending_order().
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AsyncSessionLocal
from app.models.order import Order

log = structlog.get_logger(__name__)

# Club points: 1 point per 1,000 CDF spent
_POINTS_PER_CDF = 1 / 1000

# Valid state transitions (state machine)
_VALID_TRANSITIONS: dict[str, set[str]] = {
    "pending": set(),
    "confirmed": set(),
    "preparing": set(),
    "delivering": set(),
    "delivered": set(),        # Terminal state
    "cancelled": set(),        # Terminal state
}

class InvalidOrderTransition(ValueError):
    """Raised when a state transition is not permitted by the state machine."""


class PaymentPrerequisiteUnavailable(InvalidOrderTransition):
    """Payment/Stock proof cannot yet be established by this legacy path."""


class DispatchPrerequisiteUnavailable(PaymentPrerequisiteUnavailable):
    """Dispatch must use the guarded human handoff workflow."""


class DeliveryConfirmationUnavailable(PaymentPrerequisiteUnavailable):
    """Customer delivery confirmation is not implemented."""


async def update_order_status(
    session: AsyncSession,
    *,
    order_id: uuid.UUID,
    new_status: str,
    expected_updated_at: datetime | None = None,
    idempotency_key: uuid.UUID | None = None,
    actor_kind: str | None = None,
    actor_display_name: str | None = None,
    request_id: str | None = None,
    actor_account_id: uuid.UUID | None = None,
    actor_role: str | None = None,
) -> Order:
    """
    Delegate preparation and reject all other protected lifecycle writes.

    Payment confirmation fails closed. Preparation delegates to the guarded
    contract only when authoritative prerequisites can be proved. Dispatch and
    delivery confirmation fail closed so legacy callers cannot bypass evidence.

    Cancellation is deliberately excluded. It requires the authorized,
    payment-aware, audited Order cancellation service.

    Raises:
        InvalidOrderTransition if the transition is not allowed.
    """
    if new_status == "cancelled":
        raise InvalidOrderTransition(
            "Cancellation must use the authorized Order cancellation endpoint."
        )
    if new_status == "confirmed":
        raise PaymentPrerequisiteUnavailable(
            "Order confirmation requires an authoritative verified Payment."
        )
    if new_status == "preparing":
        if (
            expected_updated_at is None
            or idempotency_key is None
            or actor_kind is None
            or actor_display_name is None
        ):
            raise PaymentPrerequisiteUnavailable(
                "Order preparation requires verified Payment and full Stock allocation proof."
            )
        from app.modules.m7_conversion.order_fulfillment import (
            prepare_confirmed_order,
        )

        result = await prepare_confirmed_order(
            session,
            order_id=order_id,
            expected_updated_at=expected_updated_at,
            idempotency_key=idempotency_key,
            actor_kind=actor_kind,
            actor_display_name=actor_display_name,
            request_id=request_id,
            actor_account_id=actor_account_id,
            actor_role=actor_role,
        )
        return result.order
    if new_status == "delivering":
        raise DispatchPrerequisiteUnavailable(
            "Order dispatch requires the authorized handoff and Stock consumption workflow."
        )
    if new_status == "delivered":
        raise DeliveryConfirmationUnavailable(
            "Customer delivery confirmation requires a separate guarded workflow."
        )

    result = await session.execute(
        select(Order)
        .where(Order.order_id == order_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
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

    if new_status == "delivered":
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
    Fail closed until a DRC provider contract and send gate are approved.
    """
    # No provider has an approved DRC merchant contract or an authorized send
    # configuration. This also protects direct service/Celery invocation.
    return {"status": "blocked", "reason": "payment_provider_not_configured"}


# ── Callback processing (called by Celery) ───────────────────────────────────

async def process_callback(
    *,
    payment_id: str,
    provider: str,
    status: str,
    idempotency_key: str,
) -> dict[str, Any]:
    """Fail closed until authoritative DRC settlement verification exists."""
    # The generic callback has no approved provider authenticity or settlement
    # verification contract. A signed notification is not proof of settlement.
    return {"status": "blocked", "reason": "payment_verification_unavailable"}
