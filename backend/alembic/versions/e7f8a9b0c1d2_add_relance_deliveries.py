"""Add one offline delivery outcome and outbound identity per candidate.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e7f8a9b0c1d2"
down_revision: Union[str, None] = "d6e7f8a9b0c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint("chk_msg_operator_authorship", "messages", schema="mbb", type_="check")
    op.create_check_constraint(
        "chk_msg_operator_authorship", "messages",
        "(operator_author_account_id IS NULL AND author_display_name IS NULL "
        "AND accepted_ownership_version IS NULL "
        "AND ((delivery_state IS NULL AND delivery_state_timestamp IS NULL) "
        "OR (direction = 'outbound' AND content_type = 'text' "
        "AND delivery_state IS NOT NULL AND delivery_state_timestamp IS NOT NULL))) OR "
        "(operator_author_account_id IS NOT NULL "
        "AND char_length(btrim(author_display_name)) BETWEEN 1 AND 100 "
        "AND accepted_ownership_version > 0 "
        "AND ((delivery_state IS NULL AND delivery_state_timestamp IS NULL) "
        "OR (delivery_state IS NOT NULL AND delivery_state_timestamp IS NOT NULL)) "
        "AND direction = 'outbound' AND content_type = 'text')",
        schema="mbb",
    )
    op.create_table(
        "relance_deliveries",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mbb.relance_candidates.candidate_id", ondelete="CASCADE"),
                  primary_key=True),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mbb.messages.message_id", ondelete="RESTRICT")),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(50)),
        sa.Column("provider_message_id", sa.String(100)),
        sa.Column("dispatch_started_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.CheckConstraint("status IN ('prepared', 'sent', 'failed', 'uncertain', 'cancelled')",
                           name="chk_relance_delivery_status"),
        sa.UniqueConstraint("outbound_message_id", name="uq_relance_delivery_message"),
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_table("relance_deliveries", schema="mbb")
    op.drop_constraint("chk_msg_operator_authorship", "messages", schema="mbb", type_="check")
    op.create_check_constraint(
        "chk_msg_operator_authorship", "messages",
        "(operator_author_account_id IS NULL AND author_display_name IS NULL "
        "AND accepted_ownership_version IS NULL AND delivery_state IS NULL "
        "AND delivery_state_timestamp IS NULL) OR "
        "(operator_author_account_id IS NOT NULL "
        "AND char_length(btrim(author_display_name)) BETWEEN 1 AND 100 "
        "AND accepted_ownership_version > 0 "
        "AND ((delivery_state IS NULL AND delivery_state_timestamp IS NULL) "
        "OR (delivery_state IS NOT NULL AND delivery_state_timestamp IS NOT NULL)) "
        "AND direction = 'outbound' AND content_type = 'text')",
        schema="mbb",
    )
