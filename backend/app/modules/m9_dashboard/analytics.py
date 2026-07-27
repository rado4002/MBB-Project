"""
app/modules/m9_dashboard/analytics.py — Funnel + relance performance queries.

Direct SQL queries against PostgreSQL. Results cached in Redis for dashboard speed.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import structlog
from sqlalchemy import func, select, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.lead import Lead
from app.models.order import Order
from app.models.relance import Relance

log = structlog.get_logger(__name__)


async def get_funnel_metrics(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """
    Compute conversion funnel metrics for a date range.
    Stages: conversations → leads → qualified (warm+hot) → converted (orders)
    """
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(period_end, datetime.max.time(), tzinfo=timezone.utc)

    # Total conversations in period
    total_convos = (await session.execute(
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.created_at >= start_dt)
        .where(Conversation.created_at <= end_dt)
    )).scalar() or 0

    # Total leads created in period
    total_leads = (await session.execute(
        select(func.count())
        .select_from(Lead)
        .where(Lead.created_at >= start_dt)
        .where(Lead.created_at <= end_dt)
    )).scalar() or 0

    # Qualified leads (warm or hot)
    qualified = (await session.execute(
        select(func.count())
        .select_from(Lead)
        .where(Lead.created_at >= start_dt)
        .where(Lead.created_at <= end_dt)
        .where(Lead.score.in_(["hot", "warm"]))
    )).scalar() or 0

    # Converted (orders placed)
    total_conversions = (await session.execute(
        select(func.count())
        .select_from(Order)
        .where(Order.created_at >= start_dt)
        .where(Order.created_at <= end_dt)
    )).scalar() or 0

    # Language breakdown
    lang_result = await session.execute(
        select(Conversation.language_detected, func.count())
        .where(Conversation.created_at >= start_dt)
        .where(Conversation.created_at <= end_dt)
        .group_by(Conversation.language_detected)
    )
    language_breakdown = {row[0]: row[1] for row in lang_result.all()}

    overall_rate = (total_conversions / total_leads * 100) if total_leads > 0 else 0.0

    stages = [
        {"stage": "conversations", "count": total_convos, "conversion_rate": 100.0},
        {"stage": "leads", "count": total_leads, "conversion_rate": (total_leads / total_convos * 100) if total_convos > 0 else 0.0},
        {"stage": "qualified", "count": qualified, "conversion_rate": (qualified / total_leads * 100) if total_leads > 0 else 0.0},
        {"stage": "converted", "count": total_conversions, "conversion_rate": overall_rate},
    ]

    return {
        "period_start": period_start,
        "period_end": period_end,
        "total_conversations": total_convos,
        "total_leads": total_leads,
        "total_conversions": total_conversions,
        "overall_conversion_rate": round(overall_rate, 2),
        "stages": stages,
        "language_breakdown": language_breakdown,
    }


async def get_relance_performance(
    session: AsyncSession,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """
    Relance engine performance: attempt-level response rates.
    """
    start_dt = datetime.combine(period_start, datetime.min.time(), tzinfo=timezone.utc)
    end_dt = datetime.combine(period_end, datetime.max.time(), tzinfo=timezone.utc)

    # Total relances in period
    total_relances = (await session.execute(
        select(func.count())
        .select_from(Relance)
        .where(Relance.created_at >= start_dt)
        .where(Relance.created_at <= end_dt)
    )).scalar() or 0

    # Per-attempt metrics
    attempt_result = await session.execute(
        select(
            Relance.attempt_number,
            func.count().label("total_sent"),
            func.sum(case((Relance.delivered_at.isnot(None), 1), else_=0)).label("total_delivered"),
            func.sum(case((Relance.response_received == True, 1), else_=0)).label("total_responded"),  # noqa: E712
        )
        .where(Relance.created_at >= start_dt)
        .where(Relance.created_at <= end_dt)
        .where(Relance.cancelled == False)  # noqa: E712
        .group_by(Relance.attempt_number)
        .order_by(Relance.attempt_number)
    )
    attempts = []
    for row in attempt_result.all():
        sent = row.total_sent or 0
        delivered = row.total_delivered or 0
        responded = row.total_responded or 0
        rate = (responded / sent * 100) if sent > 0 else 0.0
        attempts.append({
            "attempt_number": row.attempt_number,
            "total_sent": sent,
            "total_delivered": delivered,
            "total_responded": responded,
            "response_rate": round(rate, 2),
        })

    # Overall response rate
    total_responded = sum(a["total_responded"] for a in attempts)
    overall_rate = (total_responded / total_relances * 100) if total_relances > 0 else 0.0

    # Best hook type (among responded relances)
    hook_result = await session.execute(
        select(Relance.hook_type, func.count().label("cnt"))
        .where(Relance.created_at >= start_dt)
        .where(Relance.created_at <= end_dt)
        .where(Relance.response_received == True)  # noqa: E712
        .group_by(Relance.hook_type)
        .order_by(func.count().desc())
        .limit(1)
    )
    best_hook_row = hook_result.first()
    best_hook = best_hook_row[0] if best_hook_row else None

    # Opt-outs triggered (cancelled relances as proxy)
    opt_outs = (await session.execute(
        select(func.count())
        .select_from(Relance)
        .where(Relance.created_at >= start_dt)
        .where(Relance.created_at <= end_dt)
        .where(Relance.cancelled == True)  # noqa: E712
    )).scalar() or 0

    return {
        "period_start": period_start,
        "period_end": period_end,
        "total_relances": total_relances,
        "overall_response_rate": round(overall_rate, 2),
        "attempts": attempts,
        "best_hook_type": best_hook,
        "opt_out_triggered": opt_outs,
    }
