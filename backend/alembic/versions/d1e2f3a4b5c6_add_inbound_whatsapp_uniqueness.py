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
    duplicate = op.get_bind().execute(sa.text("""
        SELECT whatsapp_message_id
        FROM mbb.messages
        WHERE direction = 'inbound'
          AND whatsapp_message_id IS NOT NULL
          AND btrim(whatsapp_message_id) <> ''
        GROUP BY whatsapp_message_id
        HAVING COUNT(*) > 1
        LIMIT 1
    """)).first()
    if duplicate is not None:
        raise RuntimeError(
            "Duplicate non-empty inbound WhatsApp message IDs exist; "
            "manual review is required before migration"
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
