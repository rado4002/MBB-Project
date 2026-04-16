"""
EP-11: POST /api/v1/orders
EP-12: GET  /api/v1/orders/{order_id}
EP-13: PUT  /api/v1/orders/{order_id}/status
"""
import uuid

import structlog
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import DBSession, IdempotencyKey, get_current_role
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
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M7 not yet implemented")


@router.get(
    "/{order_id}",
    response_model=OrderResponse,
    dependencies=[Depends(get_current_role)],
)
async def get_order(order_id: uuid.UUID, db: DBSession):
    """Retrieve order details."""
    log.info("order.get", order_id=str(order_id))
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M7 not yet implemented")


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
    raise HTTPException(status_code=status.HTTP_501_NOT_IMPLEMENTED, detail="M7 not yet implemented")
