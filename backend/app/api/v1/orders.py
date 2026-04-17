"""
EP-11: POST /api/v1/orders
EP-12: GET  /api/v1/orders/{order_id}
EP-13: PUT  /api/v1/orders/{order_id}/status
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import DBSession, IdempotencyKey, get_current_role
from app.models.order import Order
from app.models.payment import Payment
from app.modules.m7_conversion.service import (
    create_order as svc_create_order,
    update_order_status as svc_update_order_status,
    InvalidOrderTransition,
)
from app.schemas.common import PaymentMethod
from app.schemas.orders import (
    OrderCreate,
    OrderResponse,
    OrderStatusResponse,
    OrderStatusUpdate,
)

log = structlog.get_logger()
router = APIRouter(prefix="/orders", tags=["M7 — Conversion Engine"])


@router.post(
    "",
    response_model=OrderResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(get_current_role)],
)
async def create_order(
    body: OrderCreate,
    db: DBSession,
    idempotency_key: IdempotencyKey,
):
    """Create a new order from a converted lead."""
    log.info("order.create", lead_id=str(body.lead_id))

    # Find customer phone from lead
    from app.models.lead import Lead
    lead_result = await db.execute(select(Lead).where(Lead.lead_id == body.lead_id))
    lead = lead_result.scalar_one_or_none()
    if lead is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lead not found")

    items = [
        {"product_id": item.product_id, "quantity": item.quantity, "unit_price_cdf": float(item.unit_price_cdf)}
        for item in body.items
    ]

    order = await svc_create_order(
        db,
        lead_id=body.lead_id,
        customer_phone=lead.customer_id,
        items=items,
        delivery_zone=body.delivery_zone,
        payment_method=body.payment_method,
        idempotency_key=idempotency_key,
    )

    return _order_to_response(order, body.payment_method)


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

    payment_method = _infer_payment_method(order)
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


def _order_to_response(order: Order, payment_method: PaymentMethod) -> OrderResponse:
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


def _infer_payment_method(order: Order) -> PaymentMethod:
    """Infer PaymentMethod enum from stored payment_type string."""
    mapping = {
        "mobile_money": PaymentMethod.orange_money,
        "bank_transfer": PaymentMethod.bank_transfer,
        "cod": PaymentMethod.cash,
    }
    return mapping.get(order.payment_type, PaymentMethod.cash)
