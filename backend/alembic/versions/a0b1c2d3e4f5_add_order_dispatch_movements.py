"""Add guarded Order dispatch evidence to Stock movements.

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "stock_movements",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "stock_movements",
        sa.Column("dispatched_quantity", sa.Integer(), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "stock_movements",
        sa.Column("handoff_reference", sa.String(length=100), nullable=True),
        schema="mbb",
    )
    op.add_column(
        "stock_movements",
        sa.Column("dispatch_request_fingerprint", sa.String(length=64), nullable=True),
        schema="mbb",
    )
    op.create_foreign_key(
        "fk_stock_movements_order_allocation",
        "stock_movements",
        "stock_allocations",
        ["order_id"],
        ["order_id"],
        source_schema="mbb",
        referent_schema="mbb",
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "chk_stock_movements_kind", "stock_movements", schema="mbb", type_="check"
    )
    op.drop_constraint(
        "chk_stock_movements_shape", "stock_movements", schema="mbb", type_="check"
    )
    op.create_check_constraint(
        "chk_stock_movements_kind",
        "stock_movements",
        "operation_kind IN ('receive', 'adjust', 'dispatch')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_stock_movements_shape",
        "stock_movements",
        "(operation_kind = 'receive' AND operation_key IS NOT NULL "
        "AND before_quantity IS NOT NULL AND received_amount IS NOT NULL "
        "AND received_amount > 0 AND after_quantity IS NOT NULL "
        "AND corrected_quantity IS NULL AND after_quantity = before_quantity + received_amount "
        "AND order_id IS NULL AND dispatched_quantity IS NULL "
        "AND handoff_reference IS NULL AND dispatch_request_fingerprint IS NULL) "
        "OR (operation_kind = 'adjust' AND received_amount IS NULL "
        "AND corrected_quantity IS NOT DISTINCT FROM after_quantity "
        "AND reason IS NOT NULL AND length(btrim(reason)) > 0 "
        "AND order_id IS NULL AND dispatched_quantity IS NULL "
        "AND handoff_reference IS NULL AND dispatch_request_fingerprint IS NULL) "
        "OR (operation_kind = 'dispatch' AND operation_key IS NOT NULL "
        "AND order_id IS NOT NULL AND before_quantity IS NOT NULL "
        "AND dispatched_quantity > 0 AND after_quantity IS NOT NULL "
        "AND after_quantity = before_quantity - dispatched_quantity "
        "AND received_amount IS NULL AND corrected_quantity IS NULL AND reason IS NULL "
        "AND handoff_reference IS NOT NULL AND length(btrim(handoff_reference)) > 0 "
        "AND dispatch_request_fingerprint IS NOT NULL "
        "AND dispatch_request_fingerprint ~ '^[0-9a-f]{64}$')",
        schema="mbb",
    )
    op.create_index(
        "uq_stock_movements_dispatch_order",
        "stock_movements",
        ["order_id"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("operation_kind = 'dispatch'"),
    )
    op.create_index(
        "uq_stock_movements_dispatch_operation_key",
        "stock_movements",
        ["operation_key"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("operation_kind = 'dispatch'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_stock_movements_dispatch_operation_key",
        table_name="stock_movements",
        schema="mbb",
    )
    op.drop_index(
        "uq_stock_movements_dispatch_order",
        table_name="stock_movements",
        schema="mbb",
    )
    op.drop_constraint(
        "chk_stock_movements_shape", "stock_movements", schema="mbb", type_="check"
    )
    op.drop_constraint(
        "chk_stock_movements_kind", "stock_movements", schema="mbb", type_="check"
    )
    op.create_check_constraint(
        "chk_stock_movements_kind",
        "stock_movements",
        "operation_kind IN ('receive', 'adjust')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_stock_movements_shape",
        "stock_movements",
        "(operation_kind = 'receive' AND operation_key IS NOT NULL "
        "AND before_quantity IS NOT NULL AND received_amount IS NOT NULL "
        "AND received_amount > 0 AND after_quantity IS NOT NULL "
        "AND corrected_quantity IS NULL AND after_quantity = before_quantity + received_amount) "
        "OR (operation_kind = 'adjust' AND received_amount IS NULL "
        "AND corrected_quantity IS NOT DISTINCT FROM after_quantity "
        "AND reason IS NOT NULL AND length(btrim(reason)) > 0)",
        schema="mbb",
    )
    op.drop_constraint(
        "fk_stock_movements_order_allocation",
        "stock_movements",
        schema="mbb",
        type_="foreignkey",
    )
    op.drop_column("stock_movements", "dispatch_request_fingerprint", schema="mbb")
    op.drop_column("stock_movements", "handoff_reference", schema="mbb")
    op.drop_column("stock_movements", "dispatched_quantity", schema="mbb")
    op.drop_column("stock_movements", "order_id", schema="mbb")
