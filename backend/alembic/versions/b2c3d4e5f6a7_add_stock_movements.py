"""Add durable stock operation evidence.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "stock_movements",
        sa.Column("movement_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("sellable_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_kind", sa.String(10), nullable=False),
        sa.Column("before_quantity", sa.Integer(), nullable=True),
        sa.Column("before_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("received_amount", sa.Integer(), nullable=True),
        sa.Column("corrected_quantity", sa.Integer(), nullable=True),
        sa.Column("after_quantity", sa.Integer(), nullable=True),
        sa.Column("reason", sa.String(500), nullable=True),
        sa.Column("actor_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_key", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("movement_id"),
        sa.ForeignKeyConstraint(["sellable_item_id"], ["mbb.sellable_items.sellable_item_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_account_id"], ["mbb.operator_accounts.account_id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("sellable_item_id", "operation_key", name="uq_stock_movements_item_operation_key"),
        sa.CheckConstraint("operation_kind IN ('receive', 'adjust')", name="chk_stock_movements_kind"),
        sa.CheckConstraint("before_quantity IS NULL OR before_quantity >= 0", name="chk_stock_movements_before"),
        sa.CheckConstraint("after_quantity IS NULL OR after_quantity >= 0", name="chk_stock_movements_after"),
        sa.CheckConstraint(
            "(operation_kind = 'receive' AND operation_key IS NOT NULL "
            "AND before_quantity IS NOT NULL AND received_amount IS NOT NULL "
            "AND received_amount > 0 AND after_quantity IS NOT NULL "
            "AND corrected_quantity IS NULL AND after_quantity = before_quantity + received_amount) "
            "OR (operation_kind = 'adjust' AND received_amount IS NULL "
            "AND corrected_quantity IS NOT DISTINCT FROM after_quantity "
            "AND reason IS NOT NULL AND length(btrim(reason)) > 0)",
            name="chk_stock_movements_shape",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_stock_movements_item_occurred", "stock_movements",
        ["sellable_item_id", "occurred_at", "movement_id"], schema="mbb",
    )
    op.execute("""
        CREATE FUNCTION mbb.reject_stock_movement_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'stock movements are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_stock_movements_append_only
        BEFORE UPDATE OR DELETE ON mbb.stock_movements
        FOR EACH ROW EXECUTE FUNCTION mbb.reject_stock_movement_change()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_stock_movements_append_only ON mbb.stock_movements")
    op.execute("DROP FUNCTION mbb.reject_stock_movement_change()")
    op.drop_index("idx_stock_movements_item_occurred", table_name="stock_movements", schema="mbb")
    op.drop_table("stock_movements", schema="mbb")
