"""
EP-23: GET /api/v1/analytics/funnel
EP-24: GET /api/v1/analytics/relance-performance
"""
from datetime import date

import structlog
from fastapi import APIRouter, Depends, Query

from app.api.deps import DBSession, get_current_role
from app.modules.m9_dashboard import analytics as analytics_svc
from app.schemas.analytics import FunnelMetrics, RelancePerformance

log = structlog.get_logger()
router = APIRouter(prefix="/analytics", tags=["M9 — Analytics"])


@router.get(
    "/funnel",
    response_model=FunnelMetrics,
    dependencies=[Depends(get_current_role)],
)
async def get_funnel_metrics(
    db: DBSession,
    period_start: date = Query(...),
    period_end: date = Query(...),
):
    """
    Conversion funnel metrics: conversations → leads → conversions,
    stage breakdown, language breakdown.
    """
    log.info("analytics.funnel", start=str(period_start), end=str(period_end))
    result = await analytics_svc.get_funnel_metrics(db, period_start, period_end)
    return FunnelMetrics(**result)


@router.get(
    "/relance-performance",
    response_model=RelancePerformance,
    dependencies=[Depends(get_current_role)],
)
async def get_relance_performance(
    db: DBSession,
    period_start: date = Query(...),
    period_end: date = Query(...),
):
    """
    Relance engine performance: attempt-level response rates, best hook types,
    opt-outs triggered.
    """
    log.info("analytics.relance", start=str(period_start), end=str(period_end))
    result = await analytics_svc.get_relance_performance(db, period_start, period_end)
    return RelancePerformance(**result)
