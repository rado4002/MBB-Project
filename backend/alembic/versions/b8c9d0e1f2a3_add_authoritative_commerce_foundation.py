"""add authoritative commerce foundation

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b8c9d0e1f2a3"
down_revision: Union[str, None] = "a7b8c9d0e1f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "products",
        sa.Column(
            "product_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("category_code", sa.String(50), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
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
        sa.PrimaryKeyConstraint("product_id", name="pk_products"),
        sa.CheckConstraint(
            "char_length(btrim(name)) BETWEEN 1 AND 200",
            name="chk_products_name_length",
        ),
        sa.CheckConstraint(
            "category_code ~ '^[a-z][a-z0-9_]{0,49}$'",
            name="chk_products_category_code",
        ),
        sa.CheckConstraint(
            "char_length(btrim(description)) BETWEEN 1 AND 4000",
            name="chk_products_description_length",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_products_active_category",
        "products",
        ["active", "category_code"],
        schema="mbb",
    )
    op.create_index("idx_products_name", "products", ["name"], schema="mbb")

    op.create_table(
        "sellable_items",
        sa.Column(
            "sellable_item_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("model_label", sa.String(100), nullable=True),
        sa.Column("sku", sa.String(64), nullable=True),
        sa.Column(
            "attributes",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.text("TRUE")),
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
        sa.PrimaryKeyConstraint("sellable_item_id", name="pk_sellable_items"),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["mbb.products.product_id"],
            name="fk_sellable_items_product_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "model_label IS NULL OR char_length(btrim(model_label)) BETWEEN 1 AND 100",
            name="chk_sellable_items_model_label_length",
        ),
        sa.CheckConstraint(
            "sku IS NULL OR (sku = upper(btrim(sku)) "
            "AND sku ~ '^[A-Z0-9][A-Z0-9._-]{0,63}$')",
            name="chk_sellable_items_sku_format",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name="chk_sellable_items_attributes_object",
        ),
        sa.CheckConstraint(
            "octet_length(attributes::text) <= 4096",
            name="chk_sellable_items_attributes_size",
        ),
        sa.CheckConstraint(
            "NOT jsonb_path_exists(attributes, "
            "'$.* ? (@.type() == \"object\" || @.type() == \"array\" || "
            "@.type() == \"null\" || @.type() == \"number\" && @ % 1 != 0)')",
            name="chk_sellable_items_attributes_primitives",
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_sellable_items_sku",
        "sellable_items",
        ["sku"],
        unique=True,
        postgresql_where=sa.text("sku IS NOT NULL"),
        schema="mbb",
    )
    op.create_index(
        "idx_sellable_items_product_active",
        "sellable_items",
        ["product_id", "active"],
        schema="mbb",
    )

    op.create_table(
        "sellable_item_prices",
        sa.Column(
            "price_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("sellable_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("amount", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column(
            "effective_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("price_id", name="pk_sellable_item_prices"),
        sa.ForeignKeyConstraint(
            ["sellable_item_id"],
            ["mbb.sellable_items.sellable_item_id"],
            name="fk_sellable_item_prices_sellable_item_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "amount > 0", name="chk_sellable_item_prices_amount_positive"
        ),
        sa.CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="chk_sellable_item_prices_currency",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= effective_at",
            name="chk_sellable_item_prices_lifecycle",
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_sellable_item_prices_current_currency",
        "sellable_item_prices",
        ["sellable_item_id", "currency"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        schema="mbb",
    )
    op.create_index(
        "idx_sellable_item_prices_history",
        "sellable_item_prices",
        ["sellable_item_id", "currency", "effective_at"],
        schema="mbb",
    )

    op.create_table(
        "exchange_rates",
        sa.Column(
            "exchange_rate_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column("quote_currency", sa.String(3), nullable=False),
        sa.Column("rate", sa.Numeric(18, 6), nullable=False),
        sa.Column(
            "effective_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("ended_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("exchange_rate_id", name="pk_exchange_rates"),
        sa.CheckConstraint("rate > 0", name="chk_exchange_rates_rate_positive"),
        sa.CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'",
            name="chk_exchange_rates_currency_format",
        ),
        sa.CheckConstraint(
            "base_currency <> quote_currency",
            name="chk_exchange_rates_distinct_currencies",
        ),
        sa.CheckConstraint(
            "base_currency = 'USD' AND quote_currency = 'CDF'",
            name="chk_exchange_rates_supported_pair",
        ),
        sa.CheckConstraint(
            "ended_at IS NULL OR ended_at >= effective_at",
            name="chk_exchange_rates_lifecycle",
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_exchange_rates_current_pair",
        "exchange_rates",
        ["base_currency", "quote_currency"],
        unique=True,
        postgresql_where=sa.text("ended_at IS NULL"),
        schema="mbb",
    )
    op.create_index(
        "idx_exchange_rates_history",
        "exchange_rates",
        ["base_currency", "quote_currency", "effective_at"],
        schema="mbb",
    )

    op.create_table(
        "inventory_statuses",
        sa.Column(
            "inventory_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("sellable_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("inventory_id", name="pk_inventory_statuses"),
        sa.ForeignKeyConstraint(
            ["sellable_item_id"],
            ["mbb.sellable_items.sellable_item_id"],
            name="fk_inventory_statuses_sellable_item_id",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "sellable_item_id", name="uq_inventory_statuses_sellable_item_id"
        ),
        sa.CheckConstraint(
            "status IN ('available', 'out_of_stock', 'unknown')",
            name="chk_inventory_statuses_status",
        ),
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_table("inventory_statuses", schema="mbb")
    op.drop_index(
        "idx_exchange_rates_history", table_name="exchange_rates", schema="mbb"
    )
    op.drop_index(
        "uq_exchange_rates_current_pair", table_name="exchange_rates", schema="mbb"
    )
    op.drop_table("exchange_rates", schema="mbb")
    op.drop_index(
        "idx_sellable_item_prices_history",
        table_name="sellable_item_prices",
        schema="mbb",
    )
    op.drop_index(
        "uq_sellable_item_prices_current_currency",
        table_name="sellable_item_prices",
        schema="mbb",
    )
    op.drop_table("sellable_item_prices", schema="mbb")
    op.drop_index(
        "idx_sellable_items_product_active",
        table_name="sellable_items",
        schema="mbb",
    )
    op.drop_index(
        "uq_sellable_items_sku", table_name="sellable_items", schema="mbb"
    )
    op.drop_table("sellable_items", schema="mbb")
    op.drop_index("idx_products_name", table_name="products", schema="mbb")
    op.drop_index(
        "idx_products_active_category", table_name="products", schema="mbb"
    )
    op.drop_table("products", schema="mbb")
