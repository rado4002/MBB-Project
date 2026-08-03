"""enforce one active escalation per conversation

Revision ID: b2e2c3d4e5f6
Revises: b1e2c3d4e5f6
Create Date: 2026-08-03

The explicit preflight aborts without modifying tickets when existing active
duplicates are present. The unique partial index remains the authoritative
concurrency guard after the migration succeeds.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b2e2c3d4e5f6"
down_revision: Union[str, None] = "b1e2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    connection = op.get_bind()
    duplicate = connection.execute(
        sa.text(
            """
            SELECT conversation_id, count(*) AS active_count
            FROM mbb.escalation_tickets
            WHERE status IN ('open', 'in_progress')
            GROUP BY conversation_id
            HAVING count(*) > 1
            LIMIT 1
            """
        )
    ).mappings().first()
    if duplicate is not None:
        raise RuntimeError(
            "B2 aborted: duplicate active escalations exist; "
            "close or resolve them through an approved business process "
            "before retrying this migration"
        )

    op.create_index(
        "uq_escalation_tickets_one_active_conversation",
        "escalation_tickets",
        ["conversation_id"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("status IN ('open', 'in_progress')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_escalation_tickets_one_active_conversation",
        table_name="escalation_tickets",
        schema="mbb",
    )
