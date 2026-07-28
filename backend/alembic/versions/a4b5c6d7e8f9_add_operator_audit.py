"""add operator audit persistence

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-07-28

Downgrade is intended only while these tables are unused. Once audit events
exist, dropping them destroys security and business evidence.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a4b5c6d7e8f9"
down_revision: Union[str, None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_audit_events",
        sa.Column(
            "event_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "occurred_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("category", sa.String(20), nullable=False),
        sa.Column("actor_kind", sa.String(20), nullable=False),
        sa.Column(
            "actor_account_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("actor_display_name", sa.String(100), nullable=True),
        sa.Column("effective_role", sa.String(20), nullable=True),
        sa.Column("request_id", sa.String(100), nullable=True),
        sa.Column("action", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=True),
        sa.Column("target_id", sa.String(100), nullable=True),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("failure_code", sa.String(100), nullable=True),
        sa.Column("idempotency_reference", sa.String(100), nullable=True),
        sa.Column(
            "metadata",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("retain_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("event_id", name="pk_operator_audit_events"),
        sa.ForeignKeyConstraint(
            ["actor_account_id"],
            ["mbb.operator_accounts.account_id"],
            name="fk_operator_audit_events_actor_account_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "category IN ('business', 'security')",
            name="chk_operator_audit_events_category",
        ),
        sa.CheckConstraint(
            "actor_kind IN ('human', 'service', 'bootstrap', 'unknown')",
            name="chk_operator_audit_events_actor_kind",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded', 'denied', 'failed')",
            name="chk_operator_audit_events_outcome",
        ),
        sa.CheckConstraint(
            "effective_role IS NULL OR effective_role IN ('administrator', 'operator', 'analyst')",
            name="chk_operator_audit_events_effective_role",
        ),
        sa.CheckConstraint(
            "retain_until > occurred_at",
            name="chk_operator_audit_events_retention",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_events_occurred_at",
        "operator_audit_events",
        ["occurred_at"],
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_events_retain_until",
        "operator_audit_events",
        ["retain_until"],
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_events_category_occurred",
        "operator_audit_events",
        ["category", "occurred_at"],
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_events_actor_occurred",
        "operator_audit_events",
        ["actor_account_id", "occurred_at"],
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_events_action",
        "operator_audit_events",
        ["action"],
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_events_request_id",
        "operator_audit_events",
        ["request_id"],
        schema="mbb",
    )

    op.create_table(
        "operator_audit_security_metadata",
        sa.Column("event_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_network_fingerprint", sa.String(64), nullable=True),
        sa.Column("user_agent_fingerprint", sa.String(64), nullable=True),
        sa.Column("retain_until", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "event_id", name="pk_operator_audit_security_metadata"
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["mbb.operator_audit_events.event_id"],
            name="fk_operator_audit_security_metadata_event_id",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "source_network_fingerprint IS NULL OR source_network_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_operator_audit_security_metadata_network_fingerprint",
        ),
        sa.CheckConstraint(
            "user_agent_fingerprint IS NULL OR user_agent_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_operator_audit_security_metadata_user_agent_fingerprint",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_operator_audit_security_metadata_retain_until",
        "operator_audit_security_metadata",
        ["retain_until"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_operator_audit_security_metadata_retain_until",
        table_name="operator_audit_security_metadata",
        schema="mbb",
    )
    op.drop_table("operator_audit_security_metadata", schema="mbb")
    op.drop_index(
        "idx_operator_audit_events_request_id",
        table_name="operator_audit_events",
        schema="mbb",
    )
    op.drop_index(
        "idx_operator_audit_events_action",
        table_name="operator_audit_events",
        schema="mbb",
    )
    op.drop_index(
        "idx_operator_audit_events_actor_occurred",
        table_name="operator_audit_events",
        schema="mbb",
    )
    op.drop_index(
        "idx_operator_audit_events_category_occurred",
        table_name="operator_audit_events",
        schema="mbb",
    )
    op.drop_index(
        "idx_operator_audit_events_retain_until",
        table_name="operator_audit_events",
        schema="mbb",
    )
    op.drop_index(
        "idx_operator_audit_events_occurred_at",
        table_name="operator_audit_events",
        schema="mbb",
    )
    op.drop_table("operator_audit_events", schema="mbb")
