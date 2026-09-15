"""
EP-12: GET  /api/v1/orders/{order_id}
EP-13: PUT  /api/v1/orders/{order_id}/status
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import DBSession, get_current_role
from app.models.order import Order
from app.models.payment import Payment
from app.modules.m7_conversion.service import (
    update_order_status as svc_update_order_status,
    InvalidOrderTransition,
)
from app.schemas.common import PaymentMethod
from app.schemas.orders import (
    OrderResponse,
    OrderStatusResponse,
    OrderStatusUpdate,
)

log = structlog.get_logger()
router = APIRouter(prefix="/orders", tags=["M7 — Conversion Engine"])


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_order(order_id: uuid.UUID, db: DBSession):
    """Retrieve order details."""
    result = await db.execute(select(Order).where(Order.order_id == order_id))
    order = result.scalar_one_or_none()
    if order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")

    # Only a stored Payment identifies a selected method. Order.payment_type
    # also contains legacy schema placeholders on draft-created pending orders.
    method = await db.scalar(select(Payment.method).where(Payment.order_id == order_id))
    payment_method = PaymentMethod(method) if method is not None else None
    return _order_to_response(order, payment_method)


@router.put(
    "/{order_id}/status",
    response_model=OrderStatusResponse,
    dependencies=[Depends(get_current_role)],
)
async def update_order_status(
    order_id: uuid.UUID,
    body: OrderStatusUpdate,
    db: DBSession,
):
    """Advance order through fulfillment lifecycle."""
    log.info("order.status.update", order_id=str(order_id), status=body.status)
    try:
        order = await svc_update_order_status(db, order_id=order_id, new_status=body.status.value)
    except InvalidOrderTransition as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return OrderStatusResponse(
        order_id=order.order_id,
        status=order.status,
        updated_at=order.updated_at,
    )


def _order_to_response(order: Order, payment_method: PaymentMethod | None) -> OrderResponse:
    """Map ORM Order to OrderResponse schema."""
    items = order.items or []
    from app.schemas.orders import OrderLineItem
    return OrderResponse(
        order_id=order.order_id,
        lead_id=order.lead_id,
        status=order.status,
        items=[OrderLineItem(**item) for item in items],
        total_cdf=order.total_amount,
        delivery_zone=order.delivery_zone,
        payment_method=payment_method,
        created_at=order.created_at,
        updated_at=order.updated_at,
    )
