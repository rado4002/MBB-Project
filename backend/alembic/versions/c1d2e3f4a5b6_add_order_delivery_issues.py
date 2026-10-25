"""Add Orders-owned delivery issue reporting and triage.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "order_delivery_issues",
        sa.Column("issue_id", postgresql.UUID(as_uuid=True), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("order_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("dispatch_movement_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("delivery_confirmation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("issue_type", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=50), nullable=False),
        sa.Column("source", sa.String(length=30), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("evidence_message_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_conversation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("evidence_received_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("reported_by_account_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reporter_display_name", sa.String(length=100), nullable=False),
        sa.Column("reported_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("report_operation_key_digest", sa.String(length=64), nullable=False),
        sa.Column("report_request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=10), server_default="open", nullable=False),
        sa.Column("resolution_code", sa.String(length=30), nullable=True),
        sa.Column("resolution_reason", sa.Text(), nullable=True),
        sa.Column("closed_by_account_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("closed_by_display_name", sa.String(length=100), nullable=True),
        sa.Column("closed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("close_operation_key_digest", sa.String(length=64), nullable=True),
        sa.Column("close_request_fingerprint", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("issue_id", name="pk_order_delivery_issues"),
        sa.ForeignKeyConstraint(["order_id"], ["mbb.orders.order_id"], name="fk_order_delivery_issues_order", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["dispatch_movement_id"], ["mbb.stock_movements.movement_id"], name="fk_order_delivery_issues_dispatch", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["delivery_confirmation_id"], ["mbb.order_delivery_confirmations.confirmation_id"], name="fk_order_delivery_issues_confirmation", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_message_id"], ["mbb.messages.message_id"], name="fk_order_delivery_issues_message", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["evidence_conversation_id"], ["mbb.conversations.conversation_id"], name="fk_order_delivery_issues_conversation", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["reported_by_account_id"], ["mbb.operator_accounts.account_id"], name="fk_order_delivery_issues_reporter", ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["closed_by_account_id"], ["mbb.operator_accounts.account_id"], name="fk_order_delivery_issues_closer", ondelete="RESTRICT"),
        sa.CheckConstraint("issue_type IN ('failed', 'disputed')", name="chk_order_delivery_issues_type"),
        sa.CheckConstraint("reason_code IN ('recipient_unavailable', 'recipient_refused', 'address_problem', 'courier_problem', 'receipt_disputed', 'condition_disputed', 'item_disputed', 'other')", name="chk_order_delivery_issues_reason"),
        sa.CheckConstraint("source IN ('customer_message', 'customer_call', 'courier_report', 'staff_observation', 'other')", name="chk_order_delivery_issues_source"),
        sa.CheckConstraint("char_length(btrim(summary)) BETWEEN 10 AND 500", name="chk_order_delivery_issues_summary"),
        sa.CheckConstraint("char_length(btrim(reporter_display_name)) BETWEEN 1 AND 100 AND (closed_by_display_name IS NULL OR char_length(btrim(closed_by_display_name)) BETWEEN 1 AND 100)", name="chk_order_delivery_issues_actor_names"),
        sa.CheckConstraint("status IN ('open', 'closed')", name="chk_order_delivery_issues_status"),
        sa.CheckConstraint("resolution_code IS NULL OR resolution_code IN ('false_alarm', 'delivery_continuing')", name="chk_order_delivery_issues_resolution"),
        sa.CheckConstraint("(evidence_message_id IS NULL AND evidence_conversation_id IS NULL AND evidence_received_at IS NULL) OR (evidence_message_id IS NOT NULL AND evidence_conversation_id IS NOT NULL AND evidence_received_at IS NOT NULL)", name="chk_order_delivery_issues_evidence_shape"),
        sa.CheckConstraint("report_operation_key_digest ~ '^[0-9a-f]{64}$' AND report_request_fingerprint ~ '^[0-9a-f]{64}$' AND (close_operation_key_digest IS NULL OR close_operation_key_digest ~ '^[0-9a-f]{64}$') AND (close_request_fingerprint IS NULL OR close_request_fingerprint ~ '^[0-9a-f]{64}$')", name="chk_order_delivery_issues_fingerprints"),
        sa.CheckConstraint("(status = 'open' AND resolution_code IS NULL AND resolution_reason IS NULL AND closed_by_account_id IS NULL AND closed_by_display_name IS NULL AND closed_at IS NULL AND close_operation_key_digest IS NULL AND close_request_fingerprint IS NULL) OR (status = 'closed' AND resolution_code IS NOT NULL AND char_length(btrim(resolution_reason)) BETWEEN 10 AND 500 AND closed_by_account_id IS NOT NULL AND closed_by_display_name IS NOT NULL AND closed_at IS NOT NULL AND close_operation_key_digest IS NOT NULL AND close_request_fingerprint IS NOT NULL AND closed_at >= reported_at)", name="chk_order_delivery_issues_resolution_shape"),
        schema="mbb",
    )
    op.create_index("idx_order_delivery_issues_order_reported", "order_delivery_issues", ["order_id", "reported_at"], schema="mbb")
    op.create_index("uq_order_delivery_issues_open_order", "order_delivery_issues", ["order_id"], unique=True, postgresql_where=sa.text("status = 'open'"), schema="mbb")
    op.create_index("uq_order_delivery_issues_report_key", "order_delivery_issues", ["report_operation_key_digest"], unique=True, schema="mbb")
    op.create_index("uq_order_delivery_issues_close_key", "order_delivery_issues", ["close_operation_key_digest"], unique=True, postgresql_where=sa.text("close_operation_key_digest IS NOT NULL"), schema="mbb")
    op.execute("""
        CREATE FUNCTION mbb.guard_order_delivery_issue_history()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'order delivery issues cannot be deleted';
            END IF;
            IF OLD.status = 'closed' THEN
                RAISE EXCEPTION 'closed order delivery issues are immutable';
            END IF;
            IF OLD.issue_id IS DISTINCT FROM NEW.issue_id
               OR OLD.order_id IS DISTINCT FROM NEW.order_id
               OR OLD.dispatch_movement_id IS DISTINCT FROM NEW.dispatch_movement_id
               OR OLD.delivery_confirmation_id IS DISTINCT FROM NEW.delivery_confirmation_id
               OR OLD.issue_type IS DISTINCT FROM NEW.issue_type
               OR OLD.reason_code IS DISTINCT FROM NEW.reason_code
               OR OLD.source IS DISTINCT FROM NEW.source
               OR OLD.summary IS DISTINCT FROM NEW.summary
               OR OLD.evidence_message_id IS DISTINCT FROM NEW.evidence_message_id
               OR OLD.evidence_conversation_id IS DISTINCT FROM NEW.evidence_conversation_id
               OR OLD.evidence_received_at IS DISTINCT FROM NEW.evidence_received_at
               OR OLD.reported_by_account_id IS DISTINCT FROM NEW.reported_by_account_id
               OR OLD.reporter_display_name IS DISTINCT FROM NEW.reporter_display_name
               OR OLD.reported_at IS DISTINCT FROM NEW.reported_at
               OR OLD.report_operation_key_digest IS DISTINCT FROM NEW.report_operation_key_digest
               OR OLD.report_request_fingerprint IS DISTINCT FROM NEW.report_request_fingerprint THEN
                RAISE EXCEPTION 'order delivery issue report evidence is immutable';
            END IF;
            RETURN NEW;
        END;
        $$
    """)
    op.execute("""
        CREATE TRIGGER trg_order_delivery_issues_history
        BEFORE UPDATE OR DELETE ON mbb.order_delivery_issues
        FOR EACH ROW EXECUTE FUNCTION mbb.guard_order_delivery_issue_history()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_order_delivery_issues_history ON mbb.order_delivery_issues")
    op.execute("DROP FUNCTION IF EXISTS mbb.guard_order_delivery_issue_history()")
    op.drop_table("order_delivery_issues", schema="mbb")
