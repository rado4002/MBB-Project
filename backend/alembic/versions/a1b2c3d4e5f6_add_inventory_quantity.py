"""Add authoritative nullable sellable item stock quantity.

Revision ID: a1b2c3d4e5f6
Revises: e7f8a9b0c1d2
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "e7f8a9b0c1d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory_statuses", sa.Column("quantity", sa.Integer(), nullable=True), schema="mbb")
    op.create_check_constraint(
        "chk_inventory_statuses_quantity", "inventory_statuses",
        "quantity IS NULL OR quantity >= 0", schema="mbb",
    )
    op.execute("UPDATE mbb.inventory_statuses SET quantity = 0 WHERE status = 'out_of_stock'")
    # Previously available stock has no trustworthy count. Leave it unknown.
    # Preserve the old status value for historical inspection, never for reads.


def downgrade() -> None:
    # Current status is maintained by new writes; historical available rows stay available.
    # Downgrade cannot preserve counts because the old schema has no quantity field.
    op.drop_constraint("chk_inventory_statuses_quantity", "inventory_statuses", schema="mbb", type_="check")
    op.drop_column("inventory_statuses", "quantity", schema="mbb")
