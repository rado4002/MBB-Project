"""Add provider-independent Payment intent evidence.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column("customer_id", sa.String(length=20), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "payments",
        sa.Column(
            "intent_idempotency_key_digest", sa.String(length=64), nullable=True
        ),
        schema="mbb",
    )
    op.add_column(
        "payments",
        sa.Column("intent_request_fingerprint", sa.String(length=64), nullable=True),
        schema="mbb",
    )
    op.create_foreign_key(
        "fk_payments_customer_id",
        "payments",
        "customers",
        ["customer_id"],
        ["phone_number"],
        source_schema="mbb",
        referent_schema="mbb",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "chk_payment_intent_evidence",
        "payments",
        "(intent_idempotency_key_digest IS NULL "
        "AND intent_request_fingerprint IS NULL AND customer_id IS NULL) OR "
        "(intent_idempotency_key_digest ~ '^[0-9a-f]{64}$' "
        "AND intent_request_fingerprint ~ '^[0-9a-f]{64}$' "
        "AND customer_id IS NOT NULL)",
        schema="mbb",
    )
    op.create_index(
        "uq_payments_intent_idempotency",
        "payments",
        ["intent_idempotency_key_digest"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("intent_idempotency_key_digest IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_payments_intent_idempotency", table_name="payments", schema="mbb"
    )
    op.drop_constraint(
        "chk_payment_intent_evidence", "payments", schema="mbb", type_="check"
    )
    op.drop_constraint(
        "fk_payments_customer_id", "payments", schema="mbb", type_="foreignkey"
    )
    op.drop_column("payments", "intent_request_fingerprint", schema="mbb")
    op.drop_column("payments", "intent_idempotency_key_digest", schema="mbb")
    op.drop_column("payments", "customer_id", schema="mbb")
