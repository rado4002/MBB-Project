"""add browser operator escalation persistence

Revision ID: b1e2c3d4e5f6
Revises: a4b5c6d7e8f9
Create Date: 2026-08-03

This additive migration preserves every legacy escalation column and caller.
The operator reason is stored only on the authoritative ticket; idempotency
records contain keyed digests and result references, never the raw key or
free-text reason.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b1e2c3d4e5f6"
down_revision: Union[str, None] = "a4b5c6d7e8f9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "escalation_tickets",
        sa.Column(
            "source",
            sa.String(30),
            nullable=False,
            server_default="legacy",
        ),
        schema="mbb",
    )
    op.add_column(
        "escalation_tickets",
        sa.Column("escalation_type", sa.String(50), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "escalation_tickets",
        sa.Column("operator_reason", sa.Text(), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "escalation_tickets",
        sa.Column(
            "created_by_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        schema="mbb",
    )
    op.create_foreign_key(
        "fk_escalation_tickets_created_by_account_id",
        "escalation_tickets",
        "operator_accounts",
        ["created_by_account_id"],
        ["account_id"],
        source_schema="mbb",
        referent_schema="mbb",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "chk_esc_source",
        "escalation_tickets",
        "source IN ('legacy', 'operator_browser')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_escalation_type",
        "escalation_tickets",
        "escalation_type IS NULL OR escalation_type IN "
        "('voice_note', 'complex_issue', 'high_value_lead', 'payment_issue')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_operator_reason",
        "escalation_tickets",
        "operator_reason IS NULL OR "
        "char_length(btrim(operator_reason)) BETWEEN 10 AND 500",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_operator_browser_fields",
        "escalation_tickets",
        "source <> 'operator_browser' OR "
        "(escalation_type IS NOT NULL AND operator_reason IS NOT NULL "
        "AND created_by_account_id IS NOT NULL)",
        schema="mbb",
    )
    op.create_index(
        "idx_esc_created_by_account",
        "escalation_tickets",
        ["created_by_account_id", "created_at"],
        schema="mbb",
        postgresql_where=sa.text("created_by_account_id IS NOT NULL"),
    )

    op.create_table(
        "operator_escalation_idempotency",
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "actor_account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column(
            "reservation_token",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "ticket_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("response_status_code", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint(
            "record_id", name="pk_operator_escalation_idempotency"
        ),
        sa.UniqueConstraint(
            "actor_account_id",
            "key_digest",
            name="uq_operator_escalation_idempotency_actor_key",
        ),
        sa.ForeignKeyConstraint(
            ["actor_account_id"],
            ["mbb.operator_accounts.account_id"],
            name="fk_operator_escalation_idempotency_actor_account_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["ticket_id"],
            ["mbb.escalation_tickets.ticket_id"],
            name="fk_operator_escalation_idempotency_ticket_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_operator_escalation_idempotency_key_digest",
        ),
        sa.CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_operator_escalation_idempotency_request_fingerprint",
        ),
        sa.CheckConstraint(
            "state IN ('in_progress', 'completed')",
            name="chk_operator_escalation_idempotency_state",
        ),
        sa.CheckConstraint(
            "(state = 'in_progress' AND reservation_token IS NOT NULL "
            "AND locked_until IS NOT NULL AND ticket_id IS NULL "
            "AND response_status_code IS NULL AND completed_at IS NULL) OR "
            "(state = 'completed' AND reservation_token IS NULL "
            "AND locked_until IS NULL AND ticket_id IS NOT NULL "
            "AND response_status_code = 201 AND completed_at IS NOT NULL)",
            name="chk_operator_escalation_idempotency_result_state",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_operator_escalation_idempotency_created_at",
        "operator_escalation_idempotency",
        ["created_at"],
        schema="mbb",
    )
    op.create_index(
        "idx_operator_escalation_idempotency_ticket_id",
        "operator_escalation_idempotency",
        ["ticket_id"],
        schema="mbb",
        postgresql_where=sa.text("ticket_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "idx_operator_escalation_idempotency_ticket_id",
        table_name="operator_escalation_idempotency",
        schema="mbb",
    )
    op.drop_index(
        "idx_operator_escalation_idempotency_created_at",
        table_name="operator_escalation_idempotency",
        schema="mbb",
    )
    op.drop_table("operator_escalation_idempotency", schema="mbb")

    op.drop_index(
        "idx_esc_created_by_account",
        table_name="escalation_tickets",
        schema="mbb",
    )
    op.drop_constraint(
        "chk_esc_operator_browser_fields",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_operator_reason",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_escalation_type",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_source",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "fk_escalation_tickets_created_by_account_id",
        "escalation_tickets",
        schema="mbb",
        type_="foreignkey",
    )
    op.drop_column("escalation_tickets", "created_by_account_id", schema="mbb")
    op.drop_column("escalation_tickets", "operator_reason", schema="mbb")
    op.drop_column("escalation_tickets", "escalation_type", schema="mbb")
    op.drop_column("escalation_tickets", "source", schema="mbb")
