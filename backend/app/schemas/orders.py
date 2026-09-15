"""Schemas for EP-12, EP-13 (M7 Conversion / Orders)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.common import OrderStatus, PaymentMethod


class OrderLineItem(BaseModel):
    product_id: str = Field(..., max_length=50)
    quantity: int = Field(..., ge=1)
    unit_price_cdf: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)


class OrderResponse(BaseModel):
    order_id: uuid.UUID
    lead_id: uuid.UUID
    status: OrderStatus
    items: list[OrderLineItem]
    total_cdf: Decimal
    delivery_zone: str = Field(description="Recorded location; not a delivery commitment.")
    payment_method: PaymentMethod | None = Field(
        description="Method from the stored Payment, or null if none exists; not proof of payment success."
    )
    created_at: datetime
    updated_at: datetime


class OrderStatusUpdate(BaseModel):
    status: OrderStatus
    note: str | None = Field(None, max_length=500)


class OrderStatusResponse(BaseModel):
    order_id: uuid.UUID
    status: OrderStatus
    updated_at: datetime
