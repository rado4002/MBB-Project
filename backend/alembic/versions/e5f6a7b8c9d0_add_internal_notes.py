"""add internal conversation notes

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-06
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "internal_notes",
        sa.Column("note_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("author_display_name", sa.String(length=100), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("note_id", name="pk_internal_notes"),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["mbb.conversations.conversation_id"],
            name="fk_internal_notes_conversation_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_account_id"],
            ["mbb.operator_accounts.account_id"],
            name="fk_internal_notes_author_account_id",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "char_length(content) BETWEEN 1 AND 4096",
            name="chk_internal_notes_content_length",
        ),
        sa.CheckConstraint(
            "char_length(btrim(author_display_name)) BETWEEN 1 AND 100",
            name="chk_internal_notes_author_display_name",
        ),
        schema="mbb",
    )
    op.create_index(
        "idx_internal_notes_conversation_created_note",
        "internal_notes",
        ["conversation_id", "created_at", "note_id"],
        unique=False,
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_internal_notes_conversation_created_note",
        table_name="internal_notes",
        schema="mbb",
    )
    op.drop_table("internal_notes", schema="mbb")
