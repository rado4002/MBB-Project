"""Add no-send Relance V2 candidates.

Revision ID: c5d6e7f8a9b0
Revises: b4c5d6e7f8a9
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c5d6e7f8a9b0"
down_revision: Union[str, None] = "b4c5d6e7f8a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "relance_candidates",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mbb.leads.lead_id", ondelete="CASCADE"), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mbb.messages.message_id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("scheduled_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("cancelled_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("confirmed_sent_at", sa.TIMESTAMP(timezone=True)),
        sa.Column("created_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.CheckConstraint("attempt_number BETWEEN 1 AND 2", name="chk_relance_candidate_attempt"),
        sa.CheckConstraint("cancelled_at IS NULL OR confirmed_sent_at IS NULL",
                           name="chk_relance_candidate_terminal"),
        schema="mbb",
    )
    op.create_index(
        "uq_relance_candidate_episode_attempt", "relance_candidates",
        ["lead_id", "source_message_id", "attempt_number"], unique=True, schema="mbb",
    )
    op.create_index(
        "uq_relance_candidate_active_lead", "relance_candidates", ["lead_id"],
        unique=True, schema="mbb",
        postgresql_where=sa.text("cancelled_at IS NULL AND confirmed_sent_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_relance_candidate_active_lead", table_name="relance_candidates", schema="mbb")
    op.drop_index("uq_relance_candidate_episode_attempt", table_name="relance_candidates", schema="mbb")
    op.drop_table("relance_candidates", schema="mbb")
