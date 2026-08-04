"""add exclusive conversation ownership

Revision ID: c3d4e5f6a7b8
Revises: b2e2c3d4e5f6
Create Date: 2026-08-04
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2e2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("owner_type", sa.String(10), nullable=False, server_default="ai"),
        schema="mbb",
    )
    op.add_column(
        "conversations",
        sa.Column(
            "human_owner_account_id", postgresql.UUID(as_uuid=True), nullable=True
        ),
        schema="mbb",
    )
    op.add_column(
        "conversations",
        sa.Column(
            "ai_execution_state",
            sa.String(20),
            nullable=False,
            server_default="eligible",
        ),
        schema="mbb",
    )
    op.add_column(
        "conversations",
        sa.Column(
            "ownership_version", sa.Integer(), nullable=False, server_default="1"
        ),
        schema="mbb",
    )
    op.add_column(
        "conversations",
        sa.Column(
            "ownership_updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        schema="mbb",
    )
    op.create_foreign_key(
        "fk_conversations_human_owner_account_id",
        "conversations",
        "operator_accounts",
        ["human_owner_account_id"],
        ["account_id"],
        source_schema="mbb",
        referent_schema="mbb",
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "chk_conv_owner_type",
        "conversations",
        "owner_type IN ('ai', 'human')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_conv_ai_execution_state",
        "conversations",
        "ai_execution_state IN ('eligible', 'paused')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_conv_exclusive_owner",
        "conversations",
        "(owner_type = 'ai' AND human_owner_account_id IS NULL "
        "AND ai_execution_state = 'eligible') OR "
        "(owner_type = 'human' AND human_owner_account_id IS NOT NULL "
        "AND ai_execution_state = 'paused')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_conv_ownership_version_positive",
        "conversations",
        "ownership_version > 0",
        schema="mbb",
    )
    op.create_table(
        "conversation_ownership_idempotency",
        sa.Column(
            "record_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("actor_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key_digest", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("reservation_token", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("locked_until", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("target_owner_type", sa.String(10), nullable=False),
        sa.Column("expected_version", sa.Integer(), nullable=False),
        sa.Column("result_version", sa.Integer(), nullable=True),
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
        sa.Column("completed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('in_progress', 'completed')",
            name="chk_conversation_ownership_idempotency_state",
        ),
        sa.CheckConstraint(
            "target_owner_type IN ('ai', 'human')",
            name="chk_conversation_ownership_idempotency_target",
        ),
        sa.CheckConstraint(
            "expected_version > 0 AND (result_version IS NULL OR result_version > 0)",
            name="chk_conversation_ownership_idempotency_versions",
        ),
        sa.ForeignKeyConstraint(
            ["actor_account_id"],
            ["mbb.operator_accounts.account_id"],
            name="fk_conversation_ownership_idempotency_actor_account_id",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["mbb.conversations.conversation_id"],
            name="fk_conversation_ownership_idempotency_conversation_id",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "actor_account_id",
            "key_digest",
            name="uq_conversation_ownership_idempotency_actor_key",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_conversation_ownership_idempotency_created_at",
        "conversation_ownership_idempotency",
        ["created_at"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_conversation_ownership_idempotency_created_at",
        table_name="conversation_ownership_idempotency",
        schema="mbb",
    )
    op.drop_table("conversation_ownership_idempotency", schema="mbb")
    op.drop_constraint(
        "chk_conv_ownership_version_positive",
        "conversations",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_conv_exclusive_owner",
        "conversations",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_conv_ai_execution_state",
        "conversations",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_conv_owner_type",
        "conversations",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "fk_conversations_human_owner_account_id",
        "conversations",
        schema="mbb",
        type_="foreignkey",
    )
    op.drop_column("conversations", "ownership_updated_at", schema="mbb")
    op.drop_column("conversations", "ownership_version", schema="mbb")
    op.drop_column("conversations", "ai_execution_state", schema="mbb")
    op.drop_column("conversations", "human_owner_account_id", schema="mbb")
    op.drop_column("conversations", "owner_type", schema="mbb")
