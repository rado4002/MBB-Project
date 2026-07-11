"""add durable uniqueness for inbound WhatsApp message IDs

Revision ID: d1e2f3a4b5c6
Revises: c7d8e9f0a1b2
Create Date: 2026-07-12
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "d1e2f3a4b5c6"
down_revision: Union[str, None] = "c7d8e9f0a1b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

INDEX_NAME = "uq_messages_inbound_whatsapp_message_id"
INDEX_PREDICATE = (
    "direction = 'inbound' AND whatsapp_message_id IS NOT NULL "
    "AND btrim(whatsapp_message_id) <> ''"
)


def upgrade() -> None:
    duplicate_counts = op.get_bind().execute(sa.text("""
        SELECT
            COUNT(*) AS duplicate_group_count,
            COALESCE(SUM(row_count - 1), 0) AS excess_row_count
        FROM (
            SELECT COUNT(*) AS row_count
            FROM mbb.messages
            WHERE direction = 'inbound'
              AND whatsapp_message_id IS NOT NULL
              AND btrim(whatsapp_message_id) <> ''
            GROUP BY whatsapp_message_id
            HAVING COUNT(*) > 1
        ) AS duplicate_groups
    """)).one()
    if duplicate_counts.duplicate_group_count:
        raise RuntimeError(
            f"Cannot create {INDEX_NAME}: "
            f"{duplicate_counts.duplicate_group_count} duplicate inbound WhatsApp ID groups "
            f"and {duplicate_counts.excess_row_count} excess rows require manual review."
        )

    op.create_index(
        INDEX_NAME,
        "messages",
        ["whatsapp_message_id"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text(INDEX_PREDICATE),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="messages", schema="mbb")
