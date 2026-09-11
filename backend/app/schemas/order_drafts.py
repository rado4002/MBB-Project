"""Strict contracts for non-consequential order drafts."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictOrderDraftModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


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


class OrderDraftSnapshot(StrictOrderDraftModel):
    draft_id: uuid.UUID
    draft_version: int = Field(gt=0)
    sellable_item_id: uuid.UUID
    quantity: int = Field(ge=1, le=1000)
    unit_price_usd: Decimal
    unit_price_cdf: Decimal
    total_cdf: Decimal
    offer_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
