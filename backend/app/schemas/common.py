"""Shared base types and enums used across all schema modules."""
from enum import Enum

from pydantic import BaseModel


# ── Enums ─────────────────────────────────────────────────────────────────────
class Language(str, Enum):
    lingala = "lingala"
    french = "french"
    swahili = "swahili"


class LeadScore(str, Enum):
    hot = "hot"
    warm = "warm"
    cold = "cold"


class LeadStage(str, Enum):
    awareness = "awareness"
    consideration = "consideration"
    decision = "decision"


class LeadIntent(str, Enum):
    product_inquiry = "product_inquiry"
    support = "support"
    club_loyalty = "club_loyalty"


class ConversationStatus(str, Enum):
    active = "active"
    qualifying = "qualifying"
    nurturing = "nurturing"
    escalated = "escalated"
    converted = "converted"
    dormant = "dormant"


class ContentType(str, Enum):
    text = "text"
    voice_note = "voice_note"
    image = "image"


class OrderStatus(str, Enum):
    pending = "pending"
    confirmed = "confirmed"
    preparing = "preparing"
    delivering = "delivering"
    delivered = "delivered"
    cancelled = "cancelled"


class PaymentMethod(str, Enum):
    orange_money = "orange_money"
    airtel_money = "airtel_money"
    mpesa = "mpesa"
    bank_transfer = "bank_transfer"
    cash = "cash"


class PaymentStatus(str, Enum):
    pending = "pending"
    completed = "completed"
    failed = "failed"
    refunded = "refunded"


class EscalationPriority(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"


class EscalationStatus(str, Enum):
    open = "open"
    in_progress = "in_progress"
    resolved = "resolved"
    closed = "closed"


class MapsCategory(str, Enum):
    product_demand = "product_demand"
    silence_reason = "silence_reason"
    conversion_trigger = "conversion_trigger"
    language_usage = "language_usage"
    opt_out_reason = "opt_out_reason"


# ── Shared Response Models ────────────────────────────────────────────────────
class PaginationMeta(BaseModel):
    limit: int
    offset: int
    total: int
    has_more: bool


class ErrorResponse(BaseModel):
    error: str
    message: str
    request_id: str | None = None
