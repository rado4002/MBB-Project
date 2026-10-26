"""Strict contracts for authoritative order drafts and their resolution."""

from __future__ import annotations

import uuid
from decimal import Decimal
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictOrderDraftModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, protected_namespaces=()
    )


class OrderDraftReplyResult(StrictOrderDraftModel):
    state: Literal[
        "confirmed",
        "cancelled",
        "already_confirmed",
        "already_cancelled",
        "invalidated",
        "refreshed",
    ]
    draft_id: uuid.UUID
    draft_version: int = Field(gt=0)
    customer_text: str = Field(min_length=1, max_length=700)
    outbound_message_id: uuid.UUID
    order_id: uuid.UUID | None = None


class OrderDraftSnapshot(StrictOrderDraftModel):
    draft_id: uuid.UUID
    draft_version: int = Field(gt=0)
    sellable_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=1000)
    unit_price_usd: Decimal
    unit_price_cdf: Decimal
    total_cdf: Decimal
    offer_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")


class OperatorOrderDraftPrepareRequest(StrictOrderDraftModel):
    model_config = ConfigDict(extra="forbid", strict=False, frozen=True)

    expected_ownership_version: int = Field(gt=0)
    product_id: uuid.UUID
    sellable_item_id: uuid.UUID
    price_id: uuid.UUID
    exchange_rate_id: uuid.UUID
    inventory_updated_at: datetime
    quantity: int = Field(ge=1, le=1000)


class OperatorOrderDraftResponse(StrictOrderDraftModel):
    draft_id: uuid.UUID
    draft_version: int = Field(gt=0)
    status: Literal["awaiting_confirmation", "confirmed", "cancelled", "invalidated"]
    product_id: uuid.UUID
    sellable_item_id: uuid.UUID
    product_name: str
    model_label: str | None
    quantity: int = Field(ge=1, le=1000)
    unit_price_usd: Decimal
    unit_price_cdf: Decimal
    total_cdf: Decimal
    confirmation_code: str = Field(pattern=r"^[A-F0-9]{8}$")
    required_customer_reply: str | None
    customer_confirmation_text: str
    presented_to_customer: Literal[False] = False
    replayed: bool = False
