"""Schemas for EP-14 (M7 Payment callbacks — Mobile Money)."""
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from app.schemas.common import PaymentMethod, PaymentStatus


class PaymentCallback(BaseModel):
    """
    Webhook body sent by Mobile Money provider (Orange, Airtel, M-Pesa).
    HMAC-SHA256 verified at the router layer before this model is used.
    """
    transaction_id: str = Field(..., max_length=100)
    order_id: uuid.UUID
    provider: PaymentMethod
    status: PaymentStatus
    amount_cdf: Decimal = Field(..., gt=0, max_digits=12, decimal_places=2)
    customer_phone: str = Field(..., max_length=20)
    timestamp: datetime
    provider_reference: str | None = Field(None, max_length=100)


class PaymentCallbackResponse(BaseModel):
    order_id: uuid.UUID
    status: PaymentStatus
    acknowledged_at: datetime
