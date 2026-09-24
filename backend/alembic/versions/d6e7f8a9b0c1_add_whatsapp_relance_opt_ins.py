"""Add operator-verified WhatsApp Relance opt-in evidence.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "d6e7f8a9b0c1"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "whatsapp_relance_opt_ins",
        sa.Column("opt_in_id", postgresql.UUID(as_uuid=True), primary_key=True,
                  server_default=sa.text("gen_random_uuid()")),
        sa.Column("customer_id", sa.String(20),
                  sa.ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"), nullable=False),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mbb.messages.message_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("verified_by_operator_account_id", postgresql.UUID(as_uuid=True),
                  sa.ForeignKey("mbb.operator_accounts.account_id", ondelete="RESTRICT"), nullable=False),
        sa.Column("granted_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.TIMESTAMP(timezone=True), nullable=False,
                  server_default=sa.text("NOW()")),
        sa.UniqueConstraint("source_message_id", name="uq_whatsapp_relance_opt_in_message"),
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_table("whatsapp_relance_opt_ins", schema="mbb")
