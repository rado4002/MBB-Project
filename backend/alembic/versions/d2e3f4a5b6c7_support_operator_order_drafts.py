"""Support operator-prepared confirmable Order drafts.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "order_drafts", "presented_outbound_message_id",
        existing_type=sa.UUID(), nullable=True, schema="mbb",
    )
    op.alter_column(
        "order_drafts", "created_by_turn_id",
        existing_type=sa.UUID(), nullable=True, schema="mbb",
    )
    op.add_column(
        "order_drafts",
        sa.Column("created_by_operator_account_id", sa.UUID(), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "order_drafts",
        sa.Column("operator_operation_key_digest", sa.String(64), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "order_drafts",
        sa.Column("operator_request_fingerprint", sa.String(64), nullable=True),
        schema="mbb",
    )
    op.create_foreign_key(
        "fk_order_drafts_created_by_operator_account_id",
        "order_drafts", "operator_accounts",
        ["created_by_operator_account_id"], ["account_id"],
        source_schema="mbb", referent_schema="mbb", ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "chk_order_drafts_creator", "order_drafts",
        "(created_by_turn_id IS NOT NULL AND "
        "created_by_operator_account_id IS NULL AND "
        "presented_outbound_message_id IS NOT NULL AND "
        "operator_operation_key_digest IS NULL AND "
        "operator_request_fingerprint IS NULL) OR "
        "(created_by_turn_id IS NULL AND "
        "created_by_operator_account_id IS NOT NULL)",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_order_drafts_operator_key_digest", "order_drafts",
        "operator_operation_key_digest IS NULL OR "
        "operator_operation_key_digest ~ '^[0-9a-f]{64}$'",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_order_drafts_operator_request_fingerprint", "order_drafts",
        "operator_request_fingerprint IS NULL OR "
        "operator_request_fingerprint ~ '^[0-9a-f]{64}$'",
        schema="mbb",
    )
    op.create_index(
        "uq_order_drafts_operator_operation_key", "order_drafts",
        ["operator_operation_key_digest"], unique=True, schema="mbb",
        postgresql_where=sa.text("operator_operation_key_digest IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_order_drafts_operator_operation_key",
        table_name="order_drafts", schema="mbb",
    )
    op.drop_constraint(
        "chk_order_drafts_operator_request_fingerprint",
        "order_drafts", type_="check", schema="mbb",
    )
    op.drop_constraint(
        "chk_order_drafts_operator_key_digest",
        "order_drafts", type_="check", schema="mbb",
    )
    op.drop_constraint(
        "chk_order_drafts_creator", "order_drafts", type_="check", schema="mbb",
    )
    op.drop_constraint(
        "fk_order_drafts_created_by_operator_account_id",
        "order_drafts", type_="foreignkey", schema="mbb",
    )
    op.drop_column("order_drafts", "operator_request_fingerprint", schema="mbb")
    op.drop_column("order_drafts", "operator_operation_key_digest", schema="mbb")
    op.drop_column("order_drafts", "created_by_operator_account_id", schema="mbb")
    op.alter_column(
        "order_drafts", "created_by_turn_id",
        existing_type=sa.UUID(), nullable=False, schema="mbb",
    )
    op.alter_column(
        "order_drafts", "presented_outbound_message_id",
        existing_type=sa.UUID(), nullable=False, schema="mbb",
    )
