"""Add immutable Order-linked Stock allocations.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "inventory_statuses",
        sa.Column(
            "allocated_quantity",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_inventory_statuses_allocated_quantity",
        "inventory_statuses",
        "allocated_quantity >= 0 AND "
        "((quantity IS NULL AND allocated_quantity = 0) OR "
        "(quantity IS NOT NULL AND allocated_quantity <= quantity))",
        schema="mbb",
    )
    op.create_table(
        "stock_allocations",
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sellable_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("before_available_quantity", sa.Integer(), nullable=False),
        sa.Column("after_available_quantity", sa.Integer(), nullable=False),
        sa.Column(
            "allocated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.CheckConstraint("quantity > 0", name="chk_stock_allocations_quantity"),
        sa.CheckConstraint(
            "before_available_quantity >= quantity "
            "AND after_available_quantity >= 0 "
            "AND after_available_quantity = before_available_quantity - quantity",
            name="chk_stock_allocations_balance",
        ),
        sa.ForeignKeyConstraint(
            ["order_id"], ["mbb.orders.order_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["sellable_item_id"],
            ["mbb.sellable_items.sellable_item_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("order_id"),
        schema="mbb",
    )
    op.create_index(
        "idx_stock_allocations_item_allocated",
        "stock_allocations",
        ["sellable_item_id", "allocated_at", "order_id"],
        schema="mbb",
    )
    op.execute("""
        CREATE FUNCTION mbb.reject_stock_allocation_change() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'stock allocations are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_stock_allocations_append_only
        BEFORE UPDATE OR DELETE ON mbb.stock_allocations
        FOR EACH ROW EXECUTE FUNCTION mbb.reject_stock_allocation_change()
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER trg_stock_allocations_append_only ON mbb.stock_allocations"
    )
    op.execute("DROP FUNCTION mbb.reject_stock_allocation_change()")
    op.drop_index(
        "idx_stock_allocations_item_allocated",
        table_name="stock_allocations",
        schema="mbb",
    )
    op.drop_table("stock_allocations", schema="mbb")
    op.drop_constraint(
        "chk_inventory_statuses_allocated_quantity",
        "inventory_statuses",
        schema="mbb",
        type_="check",
    )
    op.drop_column("inventory_statuses", "allocated_quantity", schema="mbb")
