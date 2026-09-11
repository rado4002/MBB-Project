"""add non-consequential confirmable order drafts

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-09-12
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, None] = "d0e1f2a3b4c5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "chk_ai_turn_audits_outcome",
        "ai_turn_audits",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        "chk_ai_turn_audits_outcome",
        "ai_turn_audits",
        "outcome IN ('response_generated', 'fallback_used', "
        "'handoff_requested', 'order_draft_presented', 'failed', 'no_action')",
        schema="mbb",
    )
    op.create_table(
        "order_drafts",
        sa.Column("draft_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "presented_outbound_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "resolved_by_message_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column(
            "resolution_outbound_message_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
        sa.Column("created_by_turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ownership_version", sa.Integer(), nullable=False),
        sa.Column("commercial_state_revision", sa.Integer(), nullable=False),
        sa.Column("product_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("sellable_item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("model_label", sa.String(100), nullable=True),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("price_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_price_usd", sa.Numeric(12, 2), nullable=False),
        sa.Column("price_effective_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("exchange_rate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("unit_price_cdf", sa.Numeric(24, 2), nullable=False),
        sa.Column("total_cdf", sa.Numeric(24, 2), nullable=False),
        sa.Column(
            "exchange_rate_effective_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
        ),
        sa.Column("inventory_status", sa.String(20), nullable=False),
        sa.Column("inventory_updated_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("offer_read_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("offer_fingerprint", sa.String(64), nullable=False),
        sa.Column("confirmation_code", sa.String(8), nullable=False),
        sa.Column(
            "status",
            sa.String(24),
            nullable=False,
            server_default="awaiting_confirmation",
        ),
        sa.Column("resolution_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("draft_id", "draft_version", name="pk_order_drafts"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["mbb.conversations.conversation_id"],
            name="fk_order_drafts_conversation_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_source_message_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["presented_outbound_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_presented_outbound_message_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolved_by_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_resolved_by_message_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["resolution_outbound_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_resolution_outbound_message_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["mbb.products.product_id"],
            name="fk_order_drafts_product_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["sellable_item_id"],
            ["mbb.sellable_items.sellable_item_id"],
            name="fk_order_drafts_sellable_item_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["price_id"],
            ["mbb.sellable_item_prices.price_id"],
            name="fk_order_drafts_price_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["exchange_rate_id"],
            ["mbb.exchange_rates.exchange_rate_id"],
            name="fk_order_drafts_exchange_rate_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("draft_version > 0", name="chk_order_drafts_version"),
        sa.CheckConstraint(
            "status IN ('awaiting_confirmation', 'confirmed', 'cancelled', "
            "'invalidated')",
            name="chk_order_drafts_status",
        ),
        sa.CheckConstraint(
            "quantity BETWEEN 1 AND 1000", name="chk_order_drafts_quantity"
        ),
        sa.CheckConstraint(
            "unit_price_usd > 0 AND unit_price_cdf > 0 AND total_cdf > 0",
            name="chk_order_drafts_amounts",
        ),
        sa.CheckConstraint(
            "total_cdf = unit_price_cdf * quantity",
            name="chk_order_drafts_total",
        ),
        sa.CheckConstraint(
            "ownership_version > 0 AND commercial_state_revision >= 0",
            name="chk_order_drafts_authority",
        ),
        sa.CheckConstraint(
            "confirmation_code ~ '^[A-F0-9]{8}$'",
            name="chk_order_drafts_confirmation_code",
        ),
        sa.CheckConstraint(
            "char_length(offer_fingerprint) = 64",
            name="chk_order_drafts_offer_fingerprint",
        ),
        sa.CheckConstraint(
            "(status = 'awaiting_confirmation' AND resolved_by_message_id IS NULL "
            "AND resolution_outbound_message_id IS NULL AND resolved_at IS NULL "
            "AND resolution_code IS NULL) OR "
            "(status <> 'awaiting_confirmation' AND resolved_by_message_id IS NOT NULL "
            "AND resolution_outbound_message_id IS NOT NULL AND resolved_at IS NOT NULL "
            "AND resolution_code IS NOT NULL)",
            name="chk_order_drafts_resolution",
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_order_drafts_active_conversation",
        "order_drafts",
        ["conversation_id"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("status = 'awaiting_confirmation'"),
    )
    op.create_index(
        "uq_order_drafts_confirmation_code",
        "order_drafts",
        ["confirmation_code"],
        unique=True,
        schema="mbb",
    )
    op.create_index(
        "uq_order_drafts_source_message",
        "order_drafts",
        ["source_message_id"],
        unique=True,
        schema="mbb",
    )
    op.create_index(
        "uq_order_drafts_resolved_message",
        "order_drafts",
        ["resolved_by_message_id"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("resolved_by_message_id IS NOT NULL"),
    )
    op.create_index(
        "idx_order_drafts_conversation_created",
        "order_drafts",
        ["conversation_id", "created_at"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_table("order_drafts", schema="mbb")
    op.drop_constraint(
        "chk_ai_turn_audits_outcome",
        "ai_turn_audits",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        "chk_ai_turn_audits_outcome",
        "ai_turn_audits",
        "outcome IN ('response_generated', 'fallback_used', "
        "'handoff_requested', 'failed', 'no_action')",
        schema="mbb",
    )
