"""add narrow AI turn provenance audit

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-10

The table is additive and contains no data migration. Downgrade removes only
the new AI provenance table and is destructive once AI turn audits exist.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "ai_turn_audits",
        sa.Column("turn_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("actor_type", sa.String(10), nullable=False),
        sa.Column("actor_id", sa.String(32), nullable=False),
        sa.Column("actor_display_name", sa.String(100), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "outbound_message_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        sa.Column("policy_version", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(100), nullable=True),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column(
            "exposed_capabilities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "capability_activity",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("commercial_state_revision_before", sa.Integer(), nullable=True),
        sa.Column("commercial_state_revision_after", sa.Integer(), nullable=True),
        sa.Column(
            "commercial_state_changed_fields",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("safe_code", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("turn_id", name="pk_ai_turn_audits"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["mbb.conversations.conversation_id"],
            name="fk_ai_turn_audits_conversation_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["mbb.messages.message_id"],
            name="fk_ai_turn_audits_source_message_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["outbound_message_id"],
            ["mbb.messages.message_id"],
            name="fk_ai_turn_audits_outbound_message_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint("actor_type = 'ai'", name="chk_ai_turn_audits_actor_type"),
        sa.CheckConstraint("actor_id = 'mbb_ai'", name="chk_ai_turn_audits_actor_id"),
        sa.CheckConstraint(
            "actor_display_name = 'MBB AI Assistant'",
            name="chk_ai_turn_audits_actor_display_name",
        ),
        sa.CheckConstraint(
            "outcome IN ('response_generated', 'fallback_used', "
            "'handoff_requested', 'failed', 'no_action')",
            name="chk_ai_turn_audits_outcome",
        ),
        sa.CheckConstraint(
            "commercial_state_revision_before IS NULL OR "
            "commercial_state_revision_before >= 0",
            name="chk_ai_turn_audits_revision_before",
        ),
        sa.CheckConstraint(
            "commercial_state_revision_after IS NULL OR "
            "commercial_state_revision_after >= 0",
            name="chk_ai_turn_audits_revision_after",
        ),
        sa.CheckConstraint(
            "commercial_state_revision_before IS NULL OR "
            "commercial_state_revision_after IS NULL OR "
            "commercial_state_revision_after >= commercial_state_revision_before",
            name="chk_ai_turn_audits_revision_order",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(exposed_capabilities) = 'array' AND "
            "jsonb_array_length(exposed_capabilities) <= 16",
            name="chk_ai_turn_audits_exposed_capabilities",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capability_activity) = 'array' AND "
            "jsonb_array_length(capability_activity) <= 16",
            name="chk_ai_turn_audits_capability_activity",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(commercial_state_changed_fields) = 'array' AND "
            "jsonb_array_length(commercial_state_changed_fields) <= 8",
            name="chk_ai_turn_audits_changed_fields",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_ai_turn_audits_conversation_created",
        "ai_turn_audits",
        ["conversation_id", "created_at"],
        schema="mbb",
    )
    op.create_index(
        "idx_ai_turn_audits_source_message",
        "ai_turn_audits",
        ["source_message_id"],
        schema="mbb",
    )
    op.create_index(
        "idx_ai_turn_audits_outbound_message",
        "ai_turn_audits",
        ["outbound_message_id"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_ai_turn_audits_outbound_message",
        table_name="ai_turn_audits",
        schema="mbb",
    )
    op.drop_index(
        "idx_ai_turn_audits_source_message",
        table_name="ai_turn_audits",
        schema="mbb",
    )
    op.drop_index(
        "idx_ai_turn_audits_conversation_created",
        table_name="ai_turn_audits",
        schema="mbb",
    )
    op.drop_table("ai_turn_audits", schema="mbb")
