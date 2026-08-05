"""add human operator reply fields

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-05
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("operator_author_account_id", postgresql.UUID(as_uuid=True), nullable=True), schema="mbb")
    op.add_column("messages", sa.Column("author_display_name", sa.String(100), nullable=True), schema="mbb")
    op.add_column("messages", sa.Column("accepted_ownership_version", sa.Integer(), nullable=True), schema="mbb")
    op.add_column("messages", sa.Column("delivery_state", sa.String(20), nullable=True), schema="mbb")
    op.add_column("messages", sa.Column("delivery_state_timestamp", sa.TIMESTAMP(timezone=True), nullable=True), schema="mbb")
    op.create_foreign_key("fk_messages_operator_author_account_id", "messages", "operator_accounts", ["operator_author_account_id"], ["account_id"], source_schema="mbb", referent_schema="mbb", ondelete="RESTRICT")
    op.create_check_constraint("chk_msg_delivery_state", "messages", "delivery_state IS NULL OR delivery_state IN ('accepted', 'sent', 'failed', 'uncertain')", schema="mbb")
    op.create_check_constraint(
        "chk_msg_operator_authorship",
        "messages",
        "(operator_author_account_id IS NULL AND author_display_name IS NULL AND accepted_ownership_version IS NULL AND delivery_state IS NULL AND delivery_state_timestamp IS NULL) OR (operator_author_account_id IS NOT NULL AND char_length(btrim(author_display_name)) BETWEEN 1 AND 100 AND accepted_ownership_version > 0 AND ((delivery_state IS NULL AND delivery_state_timestamp IS NULL) OR (delivery_state IS NOT NULL AND delivery_state_timestamp IS NOT NULL)) AND direction = 'outbound' AND content_type = 'text')",
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_constraint("chk_msg_operator_authorship", "messages", schema="mbb", type_="check")
    op.drop_constraint("chk_msg_delivery_state", "messages", schema="mbb", type_="check")
    op.drop_constraint("fk_messages_operator_author_account_id", "messages", schema="mbb", type_="foreignkey")
    op.drop_column("messages", "delivery_state_timestamp", schema="mbb")
    op.drop_column("messages", "delivery_state", schema="mbb")
    op.drop_column("messages", "accepted_ownership_version", schema="mbb")
    op.drop_column("messages", "author_display_name", schema="mbb")
    op.drop_column("messages", "operator_author_account_id", schema="mbb")
