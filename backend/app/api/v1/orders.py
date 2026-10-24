"""
EP-12: GET  /api/v1/orders/{order_id}
EP-13: PUT  /api/v1/orders/{order_id}/status
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select

from app.api.deps import DBSession, get_current_role, get_request_id, require_role
from app.models.order import Order
from app.models.payment import Payment
from app.modules.m7_conversion.service import (
    update_order_status as svc_update_order_status,
    InvalidOrderTransition,
)
from app.modules.m7_conversion.order_fulfillment import (
    OrderPreparationAllocationRequired,
    OrderPreparationError,
    OrderPreparationIdempotencyConflict,
    OrderPreparationNotFound,
    OrderPreparationPaymentRequired,
    OrderPreparationStale,
    OrderPreparationUnavailable,
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
    dependencies=[Depends(require_role("admin", "orchestrator"))],
    responses={
        409: {
            "description": (
                "Invalid lifecycle transition. Confirmation requires verified "
                "Payment; preparation requires full Stock allocation. Cancellation "
                "and dispatch require their authorized browser workflows; delivery "
                "confirmation is not implemented."
            )
        }
    },
)
async def update_order_status(
    order_id: uuid.UUID,
    body: OrderStatusUpdate,
    db: DBSession,
    current_role: str = Depends(get_current_role),
    request_id: str = Depends(get_request_id),
):
    """Advance eligible legacy fulfillment; payment and Stock gates fail closed."""
    log.info("order.status.update", order_id=str(order_id), status=body.status)
    try:
        order = await svc_update_order_status(
            db,
            order_id=order_id,
            new_status=body.status.value,
            expected_updated_at=body.expected_updated_at,
            idempotency_key=body.idempotency_key,
            actor_kind="service",
            actor_display_name=f"authenticated-order-api:{current_role}",
            request_id=request_id,
        )
    except OrderPreparationNotFound as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found") from exc
    except OrderPreparationUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Order preparation is temporarily unavailable",
        ) from exc
    except (
        OrderPreparationAllocationRequired,
        OrderPreparationIdempotencyConflict,
        OrderPreparationPaymentRequired,
        OrderPreparationStale,
        OrderPreparationError,
    ) as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
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
