"""Orders-owned delivery problem report and triage resolution."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, String, TIMESTAMP, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrderDeliveryIssue(Base):
    __tablename__ = "order_delivery_issues"
    __table_args__ = (
        CheckConstraint(
            "issue_type IN ('failed', 'disputed')",
            name="chk_order_delivery_issues_type",
        ),
        CheckConstraint(
            "reason_code IN ('recipient_unavailable', 'recipient_refused', "
            "'address_problem', 'courier_problem', 'receipt_disputed', "
            "'condition_disputed', 'item_disputed', 'other')",
            name="chk_order_delivery_issues_reason",
        ),
        CheckConstraint(
            "source IN ('customer_message', 'customer_call', 'courier_report', "
            "'staff_observation', 'other')",
            name="chk_order_delivery_issues_source",
        ),
        CheckConstraint(
            "char_length(btrim(summary)) BETWEEN 10 AND 500",
            name="chk_order_delivery_issues_summary",
        ),
        CheckConstraint(
            "char_length(btrim(reporter_display_name)) BETWEEN 1 AND 100 AND "
            "(closed_by_display_name IS NULL OR "
            "char_length(btrim(closed_by_display_name)) BETWEEN 1 AND 100)",
            name="chk_order_delivery_issues_actor_names",
        ),
        CheckConstraint(
            "status IN ('open', 'closed')",
            name="chk_order_delivery_issues_status",
        ),
        CheckConstraint(
            "resolution_code IS NULL OR resolution_code IN "
            "('false_alarm', 'delivery_continuing')",
            name="chk_order_delivery_issues_resolution",
        ),
        CheckConstraint(
            "(evidence_message_id IS NULL AND evidence_conversation_id IS NULL "
            "AND evidence_received_at IS NULL) OR "
            "(evidence_message_id IS NOT NULL AND evidence_conversation_id IS NOT NULL "
            "AND evidence_received_at IS NOT NULL)",
            name="chk_order_delivery_issues_evidence_shape",
        ),
        CheckConstraint(
            "report_operation_key_digest ~ '^[0-9a-f]{64}$' AND "
            "report_request_fingerprint ~ '^[0-9a-f]{64}$' AND "
            "(close_operation_key_digest IS NULL OR "
            "close_operation_key_digest ~ '^[0-9a-f]{64}$') AND "
            "(close_request_fingerprint IS NULL OR "
            "close_request_fingerprint ~ '^[0-9a-f]{64}$')",
            name="chk_order_delivery_issues_fingerprints",
        ),
        CheckConstraint(
            "(status = 'open' AND resolution_code IS NULL AND resolution_reason IS NULL "
            "AND closed_by_account_id IS NULL AND closed_by_display_name IS NULL "
            "AND closed_at IS NULL "
            "AND close_operation_key_digest IS NULL AND close_request_fingerprint IS NULL) "
            "OR (status = 'closed' AND resolution_code IS NOT NULL "
            "AND char_length(btrim(resolution_reason)) BETWEEN 10 AND 500 "
            "AND closed_by_account_id IS NOT NULL AND closed_by_display_name IS NOT NULL "
            "AND closed_at IS NOT NULL "
            "AND close_operation_key_digest IS NOT NULL "
            "AND close_request_fingerprint IS NOT NULL AND closed_at >= reported_at)",
            name="chk_order_delivery_issues_resolution_shape",
        ),
        Index("idx_order_delivery_issues_order_reported", "order_id", "reported_at"),
        Index(
            "uq_order_delivery_issues_open_order",
            "order_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
        ),
        Index(
            "uq_order_delivery_issues_report_key",
            "report_operation_key_digest",
            unique=True,
        ),
        Index(
            "uq_order_delivery_issues_close_key",
            "close_operation_key_digest",
            unique=True,
            postgresql_where=text("close_operation_key_digest IS NOT NULL"),
        ),
        {"schema": "mbb"},
    )

    issue_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.orders.order_id", ondelete="RESTRICT"), nullable=False
    )
    dispatch_movement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.stock_movements.movement_id", ondelete="RESTRICT"), nullable=False
    )
    delivery_confirmation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.order_delivery_confirmations.confirmation_id", ondelete="RESTRICT"),
        nullable=True,
    )
    issue_type: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.messages.message_id", ondelete="RESTRICT"), nullable=True
    )
    evidence_conversation_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.conversations.conversation_id", ondelete="RESTRICT"), nullable=True
    )
    evidence_received_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    reported_by_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.operator_accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    reporter_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    reported_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    report_operation_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    report_request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False, server_default="open")
    resolution_code: Mapped[str | None] = mapped_column(String(30), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    closed_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.operator_accounts.account_id", ondelete="RESTRICT"), nullable=True
    )
    closed_by_display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    close_operation_key_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    close_request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
