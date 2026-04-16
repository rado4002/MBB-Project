"""create_materialized_views — mv_daily_maps_summary and mv_funnel_metrics.

These pre-aggregated views power the M9 Analytics Dashboard (Streamlit).
Refresh is triggered by Celery Beat tasks (not pg_cron) to avoid requiring
the pg_cron extension on the VPS.

Revision ID: c7d8e9f0a1b2
Revises: b39a3ae8f092
Create Date: 2026-04-16

"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'b39a3ae8f092'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── mv_daily_maps_summary ─────────────────────────────────────────────────
    # Pre-aggregates MAPS tag counts by day/category/pattern/city/language.
    # Refreshed daily at 02:00 UTC by the Celery Beat task `refresh_maps_view`.
    # Uses WITH DATA so the view is immediately queryable on first deploy.
    op.execute("""
        CREATE MATERIALIZED VIEW mbb.mv_daily_maps_summary AS
        SELECT
            DATE_TRUNC('day', created_at) AS day,
            category,
            pattern,
            city,
            language,
            COUNT(*) AS tag_count
        FROM mbb.maps_tags
        WHERE created_at > NOW() - INTERVAL '90 days'
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY day DESC, tag_count DESC
        WITH DATA
    """)

    # UNIQUE index required for CONCURRENTLY refresh (no table lock during refresh)
    op.execute("""
        CREATE UNIQUE INDEX idx_mv_maps_day
        ON mbb.mv_daily_maps_summary (day, category, pattern, city, language)
    """)

    # ── mv_funnel_metrics ─────────────────────────────────────────────────────
    # Pre-computes conversion funnel by day/source/score/stage.
    # Refreshed daily at 02:30 UTC by Celery Beat task `refresh_funnel_view`.
    op.execute("""
        CREATE MATERIALIZED VIEW mbb.mv_funnel_metrics AS
        SELECT
            DATE_TRUNC('day', l.qualified_at) AS day,
            l.source,
            l.score,
            l.stage,
            COUNT(*)                                                          AS lead_count,
            COUNT(o.order_id)                                                 AS converted_count,
            ROUND(
                COUNT(o.order_id)::NUMERIC / NULLIF(COUNT(*), 0) * 100, 1
            )                                                                 AS conversion_rate,
            COALESCE(AVG(o.total_amount) FILTER (WHERE o.order_id IS NOT NULL), 0)
                                                                              AS avg_order_value_cdf
        FROM mbb.leads l
        LEFT JOIN mbb.orders o
            ON o.lead_id = l.lead_id
            AND o.status NOT IN ('cancelled')
        WHERE l.qualified_at > NOW() - INTERVAL '90 days'
        GROUP BY 1, 2, 3, 4
        ORDER BY day DESC
        WITH DATA
    """)

    # UNIQUE index required for CONCURRENTLY refresh
    op.execute("""
        CREATE UNIQUE INDEX idx_mv_funnel_day
        ON mbb.mv_funnel_metrics (day, source, score, stage)
    """)


def downgrade() -> None:
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mbb.mv_funnel_metrics")
    op.execute("DROP MATERIALIZED VIEW IF EXISTS mbb.mv_daily_maps_summary")
