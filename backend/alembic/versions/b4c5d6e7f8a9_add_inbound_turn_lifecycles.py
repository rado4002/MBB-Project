"""add durable inbound turn completion lifecycle

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8
Create Date: 2026-09-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a3b4c5d6e7f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inbound_turn_lifecycles",
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("ownership_version", sa.Integer(), nullable=False),
        sa.Column(
            "state", sa.String(24), nullable=False, server_default="pending"
        ),
        sa.Column("outcome_type", sa.String(24), nullable=True),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("disposition_code", sa.String(64), nullable=True),
        sa.Column("attempt_id", sa.String(64), nullable=True),
        sa.Column("claim_expires_at", sa.TIMESTAMP(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint(
            "source_message_id", name="pk_inbound_turn_lifecycles"
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["mbb.messages.message_id"],
            name="fk_inbound_turn_lifecycles_source_message_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["mbb.conversations.conversation_id"],
            name="fk_inbound_turn_lifecycles_conversation_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"],
            ["mbb.messages.message_id"],
            name="fk_inbound_turn_lifecycles_outbound_message_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'processing', 'outcome_committed', "
            "'send_completed', 'send_uncertain', 'skipped')",
            name="chk_inbound_turn_lifecycles_state",
        ),
        sa.CheckConstraint(
            "outcome_type IS NULL OR outcome_type IN "
            "('response', 'fallback', 'order_draft', 'handoff', "
            "'draft_reply', 'opt_out', 'voice_note')",
            name="chk_inbound_turn_lifecycles_outcome_type",
        ),
        sa.CheckConstraint(
            "ownership_version > 0",
            name="chk_inbound_turn_lifecycles_ownership_version",
        ),
        sa.CheckConstraint(
            "(outbound_message_id IS NULL AND outcome_type IS NULL) OR "
            "(outbound_message_id IS NOT NULL AND outcome_type IS NOT NULL)",
            name="chk_inbound_turn_lifecycles_outcome_pair",
        ),
        sa.CheckConstraint(
            "state NOT IN ('outcome_committed', 'send_completed', 'send_uncertain') "
            "OR outbound_message_id IS NOT NULL",
            name="chk_inbound_turn_lifecycles_outcome_required",
        ),
        sa.CheckConstraint(
            "char_length(attempt_id) BETWEEN 1 AND 64 OR attempt_id IS NULL",
            name="chk_inbound_turn_lifecycles_attempt_id",
        ),
        sa.CheckConstraint(
            "char_length(disposition_code) BETWEEN 1 AND 64 "
            "OR disposition_code IS NULL",
            name="chk_inbound_turn_lifecycles_disposition_code",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_inbound_turn_lifecycles_state_updated",
        "inbound_turn_lifecycles",
        ["state", "updated_at"],
        schema="mbb",
    )
    op.create_index(
        "idx_inbound_turn_lifecycles_outbound",
        "inbound_turn_lifecycles",
        ["outbound_message_id"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_inbound_turn_lifecycles_outbound",
        table_name="inbound_turn_lifecycles",
        schema="mbb",
    )
    op.drop_index(
        "idx_inbound_turn_lifecycles_state_updated",
        table_name="inbound_turn_lifecycles",
        schema="mbb",
    )
    op.drop_table("inbound_turn_lifecycles", schema="mbb")
