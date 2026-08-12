"""Add authoritative Product Media.

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
"""

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, None] = "b8c9d0e1f2a3"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.create_table(
        "product_media",
        sa.Column(
            "media_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "sellable_item_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("asset_url", sa.String(length=2048), nullable=False),
        sa.Column("alt_text", sa.String(length=500), nullable=True),
        sa.Column(
            "is_primary", sa.Boolean(), nullable=False, server_default=sa.text("FALSE")
        ),
        sa.Column(
            "display_order", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column(
            "active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")
        ),
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
        sa.PrimaryKeyConstraint("media_id", name="pk_product_media"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["mbb.products.product_id"],
            name="fk_product_media_product_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sellable_item_id"],
            ["mbb.sellable_items.sellable_item_id"],
            name="fk_product_media_sellable_item_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "(product_id IS NOT NULL AND sellable_item_id IS NULL) OR "
            "(product_id IS NULL AND sellable_item_id IS NOT NULL)",
            name="chk_product_media_exactly_one_owner",
        ),
        sa.CheckConstraint(
            "char_length(asset_url) BETWEEN 1 AND 2048 "
            "AND lower(asset_url) LIKE 'https://%'",
            name="chk_product_media_asset_url",
        ),
        sa.CheckConstraint(
            "alt_text IS NULL OR char_length(btrim(alt_text)) BETWEEN 1 AND 500",
            name="chk_product_media_alt_text_length",
        ),
        sa.CheckConstraint(
            "display_order BETWEEN 0 AND 999",
            name="chk_product_media_display_order",
        ),
        sa.CheckConstraint(
            "NOT is_primary OR active",
            name="chk_product_media_primary_active",
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_product_media_active_primary_product",
        "product_media",
        ["product_id"],
        unique=True,
        postgresql_where=sa.text(
            "product_id IS NOT NULL AND active AND is_primary"
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_product_media_active_primary_sellable_item",
        "product_media",
        ["sellable_item_id"],
        unique=True,
        postgresql_where=sa.text(
            "sellable_item_id IS NOT NULL AND active AND is_primary"
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_product_media_product_active_order",
        "product_media",
        ["product_id", "active", "display_order"],
        schema="mbb",
    )
    op.create_index(
        "idx_product_media_sellable_item_active_order",
        "product_media",
        ["sellable_item_id", "active", "display_order"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_product_media_sellable_item_active_order",
        table_name="product_media",
        schema="mbb",
    )
    op.drop_index(
        "idx_product_media_product_active_order",
        table_name="product_media",
        schema="mbb",
    )
    op.drop_index(
        "uq_product_media_active_primary_sellable_item",
        table_name="product_media",
        schema="mbb",
    )
    op.drop_index(
        "uq_product_media_active_primary_product",
        table_name="product_media",
        schema="mbb",
    )
    op.drop_table("product_media", schema="mbb")
