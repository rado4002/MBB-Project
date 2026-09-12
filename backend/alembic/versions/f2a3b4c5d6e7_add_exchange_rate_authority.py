"""add manual and automatic exchange-rate authority

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f2a3b4c5d6e7"
down_revision: Union[str, None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "exchange_rates",
        sa.Column(
            "authority_mode",
            sa.String(16),
            nullable=False,
            server_default="MANUAL",
        ),
        schema="mbb",
    )
    op.add_column(
        "exchange_rates",
        sa.Column(
            "source",
            sa.String(64),
            nullable=False,
            server_default="MBB_ADMIN",
        ),
        schema="mbb",
    )
    op.add_column(
        "exchange_rates",
        sa.Column("fetched_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "exchange_rates",
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "exchange_rates",
        sa.Column("validated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "exchange_rates",
        sa.Column(
            "validation_status",
            sa.String(24),
            nullable=False,
            server_default="ADMIN_APPROVED",
        ),
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_exchange_rates_authority_mode",
        "exchange_rates",
        "authority_mode IN ('MANUAL', 'AUTOMATIC')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_exchange_rates_validation_status",
        "exchange_rates",
        "validation_status IN ('ADMIN_APPROVED', 'VALIDATED')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_exchange_rates_provenance",
        "exchange_rates",
        "(authority_mode = 'MANUAL' AND source = 'MBB_ADMIN' "
        "AND fetched_at IS NULL AND published_at IS NULL "
        "AND validated_at IS NULL AND validation_status = 'ADMIN_APPROVED') OR "
        "(authority_mode = 'AUTOMATIC' AND source = 'EXCHANGE_RATE_API' "
        "AND fetched_at IS NOT NULL AND published_at IS NOT NULL "
        "AND validated_at IS NOT NULL AND validation_status = 'VALIDATED')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_exchange_rates_provider_time",
        "exchange_rates",
        "published_at IS NULL OR published_at <= fetched_at",
        schema="mbb",
    )
    op.drop_index(
        "uq_exchange_rates_current_pair",
        table_name="exchange_rates",
        schema="mbb",
    )
    op.create_index(
        "uq_exchange_rates_current_pair_mode",
        "exchange_rates",
        ["base_currency", "quote_currency", "authority_mode"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        schema="mbb",
    )
    op.create_table(
        "exchange_rate_authorities",
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_by_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint(
            "base_currency",
            "quote_currency",
            name="pk_exchange_rate_authorities",
        ),
        sa.CheckConstraint(
            "base_currency = 'USD' AND quote_currency = 'CDF'",
            name="chk_exchange_rate_authorities_supported_pair",
        ),
        sa.CheckConstraint(
            "mode IN ('MANUAL', 'AUTOMATIC')",
            name="chk_exchange_rate_authorities_mode",
        ),
        sa.CheckConstraint(
            "revision > 0",
            name="chk_exchange_rate_authorities_revision",
        ),
        schema="mbb",
    )
    op.execute(
        "INSERT INTO mbb.exchange_rate_authorities "
        "(base_currency, quote_currency, mode) VALUES ('USD', 'CDF', 'MANUAL')"
    )


def downgrade() -> None:
    op.execute(
        "WITH selected AS ("
        " SELECT exchange_rate_id FROM mbb.exchange_rates"
        " WHERE ended_at IS NULL"
        " ORDER BY CASE authority_mode WHEN 'MANUAL' THEN 0 ELSE 1 END,"
        " effective_at DESC, exchange_rate_id DESC LIMIT 1"
        ") UPDATE mbb.exchange_rates SET ended_at = GREATEST(NOW(), effective_at)"
        " WHERE ended_at IS NULL AND exchange_rate_id NOT IN"
        " (SELECT exchange_rate_id FROM selected)"
    )
    op.drop_table("exchange_rate_authorities", schema="mbb")
    op.drop_index(
        "uq_exchange_rates_current_pair_mode",
        table_name="exchange_rates",
        schema="mbb",
    )
    op.create_index(
        "uq_exchange_rates_current_pair",
        "exchange_rates",
        ["base_currency", "quote_currency"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        schema="mbb",
    )
    op.drop_constraint(
        "chk_exchange_rates_provider_time",
        "exchange_rates",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_exchange_rates_provenance",
        "exchange_rates",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_exchange_rates_validation_status",
        "exchange_rates",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_exchange_rates_authority_mode",
        "exchange_rates",
        schema="mbb",
        type_="check",
    )
    op.drop_column("exchange_rates", "validation_status", schema="mbb")
    op.drop_column("exchange_rates", "validated_at", schema="mbb")
    op.drop_column("exchange_rates", "published_at", schema="mbb")
    op.drop_column("exchange_rates", "fetched_at", schema="mbb")
    op.drop_column("exchange_rates", "source", schema="mbb")
    op.drop_column("exchange_rates", "authority_mode", schema="mbb")
