"""Schemas for EP-23, EP-24 (M9 Analytics Dashboard)."""
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.schemas.common import Language, LeadScore


class FunnelStageMetrics(BaseModel):
    stage: str
    count: int
    conversion_rate: float


class FunnelMetrics(BaseModel):
    period_start: date
    period_end: date
    total_conversations: int
    total_leads: int
    total_conversions: int
    overall_conversion_rate: float
    stages: list[FunnelStageMetrics]
    language_breakdown: dict[str, int]


class RelanceAttemptMetrics(BaseModel):
    attempt_number: int
    total_sent: int
    total_delivered: int
    total_responded: int
    response_rate: float


class RelancePerformance(BaseModel):
    period_start: date
    period_end: date
    total_relances: int
    overall_response_rate: float
    attempts: list[RelanceAttemptMetrics]
    best_hook_type: str | None = None
    opt_out_triggered: int
