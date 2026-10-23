"""Schemas for EP-12, EP-13 (M7 Conversion / Orders)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field, UUID4, field_validator

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
    status: OrderStatus = Field(
        description=(
            "Requested fulfillment status. The generic service blocks confirmed "
            "without verified Payment and preparing without full Stock allocation, "
            "and rejects cancelled; "
            "use the authorized Order cancellation endpoint: "
            "POST /api/v1/business/orders/{order_id}/cancel."
        )
    )
    note: str | None = Field(None, max_length=500)
    expected_updated_at: datetime | None = None
    idempotency_key: UUID4 | None = None

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value


class OrderStatusResponse(BaseModel):
    order_id: uuid.UUID
    status: OrderStatus
    updated_at: datetime
