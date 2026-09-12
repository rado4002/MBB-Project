"""bind customer-confirmed drafts to one pending order

Revision ID: a3b4c5d6e7f8
Revises: f2a3b4c5d6e7
Create Date: 2026-09-13
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a3b4c5d6e7f8"
down_revision: Union[str, None] = "f2a3b4c5d6e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_drafts",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="mbb",
    )
    op.create_foreign_key(
        "fk_order_drafts_order_id",
        "order_drafts",
        "orders",
        ["order_id"],
        ["order_id"],
        source_schema="mbb",
        referent_schema="mbb",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "chk_order_drafts_order_binding",
        "order_drafts",
        "order_id IS NULL OR status = 'confirmed'",
        schema="mbb",
    )
    op.create_index(
        "uq_order_drafts_order_id",
        "order_drafts",
        ["order_id"],
        unique=True,
        postgresql_where=sa.text("order_id IS NOT NULL"),
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "uq_order_drafts_order_id",
        table_name="order_drafts",
        schema="mbb",
    )
    op.drop_constraint(
        "chk_order_drafts_order_binding",
        "order_drafts",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "fk_order_drafts_order_id",
        "order_drafts",
        schema="mbb",
        type_="foreignkey",
    )
    op.drop_column("order_drafts", "order_id", schema="mbb")
