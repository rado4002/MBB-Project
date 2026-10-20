"""Reject Payment attachment to cancelled Orders.

Revision ID: d7e8f9a0b1c2
Revises: c4d5e6f7a8b9
"""

from typing import Sequence, Union

from alembic import op

revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
        CREATE FUNCTION mbb.reject_payment_for_cancelled_order() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE
            order_status text;
        BEGIN
            -- FOR SHARE conflicts with an Order status update and with the
            -- cancellation service's FOR UPDATE lock. The status is checked
            -- after any concurrent transaction holding that lock commits.
            SELECT status INTO order_status
            FROM mbb.orders
            WHERE order_id = NEW.order_id
            FOR SHARE;
            IF order_status = 'cancelled' THEN
                RAISE EXCEPTION 'Payment cannot be attached to a cancelled Order'
                    USING ERRCODE = '23514',
                          CONSTRAINT = 'chk_payment_order_not_cancelled';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_payment_order_not_cancelled
        BEFORE INSERT OR UPDATE OF order_id ON mbb.payments
        FOR EACH ROW EXECUTE FUNCTION mbb.reject_payment_for_cancelled_order()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER trg_payment_order_not_cancelled ON mbb.payments")
    op.execute("DROP FUNCTION mbb.reject_payment_for_cancelled_order()")
