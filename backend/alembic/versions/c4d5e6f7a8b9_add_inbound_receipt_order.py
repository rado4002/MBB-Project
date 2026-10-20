"""Serialize inbound receipt order per conversation, independent of wall time.

Revision ID: c4d5e6f7a8b9
Revises: b2c3d4e5f6a7
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TABLE holds the write lock through backfill and trigger installation.
    op.add_column(
        "conversations",
        sa.Column("last_inbound_receipt_order", sa.BigInteger(),
                  server_default=sa.text("0"), nullable=False),
        schema="mbb",
    )
    op.add_column(
        "messages", sa.Column("inbound_receipt_order", sa.BigInteger(), nullable=True),
        schema="mbb",
    )
    # Legacy timestamps are the only available ordering evidence. New writes use
    # the transaction-serialized counter below; historical commit order is unknown.
    op.execute("""
        WITH ranked AS (
            SELECT message_id,
                   row_number() OVER (
                       PARTITION BY conversation_id
                       ORDER BY created_at, timestamp, message_id
                   ) AS receipt_order
            FROM mbb.messages WHERE direction = 'inbound'
        )
        UPDATE mbb.messages AS m
        SET inbound_receipt_order = ranked.receipt_order
        FROM ranked WHERE m.message_id = ranked.message_id
    """)
    op.execute("""
        UPDATE mbb.conversations AS c
        SET last_inbound_receipt_order = counts.last_order
        FROM (
            SELECT conversation_id, max(inbound_receipt_order) AS last_order
            FROM mbb.messages WHERE direction = 'inbound'
            GROUP BY conversation_id
        ) AS counts
        WHERE c.conversation_id = counts.conversation_id
    """)
    op.create_check_constraint(
        "chk_msg_inbound_receipt_order", "messages",
        "(direction = 'inbound' AND inbound_receipt_order IS NOT NULL "
        "AND inbound_receipt_order > 0) OR "
        "(direction = 'outbound' AND inbound_receipt_order IS NULL)",
        schema="mbb",
    )
    op.create_index(
        "uq_msg_conversation_inbound_receipt_order", "messages",
        ["conversation_id", "inbound_receipt_order"], unique=True,
        schema="mbb", postgresql_where=sa.text("direction = 'inbound'"),
    )
    op.execute("""
        CREATE FUNCTION mbb.assign_inbound_receipt_order() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                IF NEW.conversation_id IS DISTINCT FROM OLD.conversation_id
                   OR NEW.direction IS DISTINCT FROM OLD.direction
                   OR NEW.inbound_receipt_order IS DISTINCT FROM OLD.inbound_receipt_order THEN
                    RAISE EXCEPTION 'message receipt identity is immutable';
                END IF;
                RETURN NEW;
            END IF;
            IF NEW.direction = 'inbound' THEN
                UPDATE mbb.conversations
                SET last_inbound_receipt_order = last_inbound_receipt_order + 1
                WHERE conversation_id = NEW.conversation_id
                RETURNING last_inbound_receipt_order
                INTO NEW.inbound_receipt_order;
                IF NOT FOUND THEN
                    RAISE EXCEPTION 'inbound conversation is missing';
                END IF;
            ELSE
                NEW.inbound_receipt_order := NULL;
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_assign_inbound_receipt_order
        BEFORE INSERT OR UPDATE OF conversation_id, direction, inbound_receipt_order
        ON mbb.messages
        FOR EACH ROW EXECUTE FUNCTION mbb.assign_inbound_receipt_order()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_assign_inbound_receipt_order ON mbb.messages")
    op.execute("DROP FUNCTION mbb.assign_inbound_receipt_order()")
    op.drop_index("uq_msg_conversation_inbound_receipt_order", table_name="messages", schema="mbb")
    op.drop_constraint("chk_msg_inbound_receipt_order", "messages", schema="mbb", type_="check")
    op.drop_column("messages", "inbound_receipt_order", schema="mbb")
    op.drop_column("conversations", "last_inbound_receipt_order", schema="mbb")
