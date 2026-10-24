"""Add guarded Order delivery confirmation evidence.

Revision ID: b0c1d2e3f4a5
Revises: a0b1c2d3e4f5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_delivery_confirmations",
        sa.Column(
            "confirmation_id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("evidence_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "evidence_conversation_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column(
            "dispatch_movement_id", postgresql.UUID(as_uuid=True), nullable=False
        ),
        sa.Column("evidence_inbound_receipt_order", sa.BigInteger(), nullable=False),
        sa.Column("evidence_received_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("dispatch_occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("evidence_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("actor_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("operation_key_digest", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("confirmed_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "confirmation_id", name="pk_order_delivery_confirmations"
        ),
        sa.UniqueConstraint(
            "order_id", name="uq_order_delivery_confirmations_order_id"
        ),
        sa.ForeignKeyConstraint(
            ["order_id"],
            ["mbb.orders.order_id"],
            name="fk_order_delivery_confirmations_order",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_delivery_confirmations_message",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["evidence_conversation_id"],
            ["mbb.conversations.conversation_id"],
            name="fk_order_delivery_confirmations_conversation",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["dispatch_movement_id"],
            ["mbb.stock_movements.movement_id"],
            name="fk_order_delivery_confirmations_dispatch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["actor_account_id"],
            ["mbb.operator_accounts.account_id"],
            name="fk_order_delivery_confirmations_actor",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "evidence_inbound_receipt_order > 0",
            name="chk_order_delivery_confirmation_receipt_order",
        ),
        sa.CheckConstraint(
            "evidence_received_at > dispatch_occurred_at",
            name="chk_order_delivery_confirmation_post_dispatch",
        ),
        sa.CheckConstraint(
            "confirmed_at >= evidence_received_at",
            name="chk_order_delivery_confirmation_timestamps",
        ),
        sa.CheckConstraint(
            "operation_key_digest ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND evidence_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_order_delivery_confirmation_fingerprints",
        ),
        schema="mbb",
    )
    for name, column in (
        ("uq_order_delivery_confirmation_evidence_message", "evidence_message_id"),
        ("uq_order_delivery_confirmation_dispatch_movement", "dispatch_movement_id"),
        ("uq_order_delivery_confirmation_operation_key", "operation_key_digest"),
    ):
        op.create_index(
            name, "order_delivery_confirmations", [column], unique=True, schema="mbb"
        )
    op.execute("""
        CREATE FUNCTION mbb.prevent_order_delivery_confirmation_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'order delivery confirmations are append-only';
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_order_delivery_confirmations_append_only
        BEFORE UPDATE OR DELETE ON mbb.order_delivery_confirmations
        FOR EACH ROW EXECUTE FUNCTION
        mbb.prevent_order_delivery_confirmation_mutation()
    """)


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_order_delivery_confirmations_append_only "
        "ON mbb.order_delivery_confirmations"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS mbb.prevent_order_delivery_confirmation_mutation()"
    )
    op.drop_table("order_delivery_confirmations", schema="mbb")
