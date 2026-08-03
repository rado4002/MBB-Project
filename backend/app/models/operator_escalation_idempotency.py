"""Durable, digest-only idempotency records for browser escalations."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OperatorEscalationIdempotency(Base):
    __tablename__ = "operator_escalation_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "actor_account_id",
            "key_digest",
            name="uq_operator_escalation_idempotency_actor_key",
        ),
        CheckConstraint(
            "key_digest ~ '^[0-9a-f]{64}$'",
            name="chk_operator_escalation_idempotency_key_digest",
        ),
        CheckConstraint(
            "request_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_operator_escalation_idempotency_request_fingerprint",
        ),
        CheckConstraint(
            "state IN ('in_progress', 'completed')",
            name="chk_operator_escalation_idempotency_state",
        ),
        CheckConstraint(
            "(state = 'in_progress' AND reservation_token IS NOT NULL "
            "AND locked_until IS NOT NULL AND ticket_id IS NULL "
            "AND response_status_code IS NULL AND completed_at IS NULL) OR "
            "(state = 'completed' AND reservation_token IS NULL "
            "AND locked_until IS NULL AND ticket_id IS NOT NULL "
            "AND response_status_code = 201 AND completed_at IS NOT NULL)",
            name="chk_operator_escalation_idempotency_result_state",
        ),
        Index("idx_operator_escalation_idempotency_created_at", "created_at"),
        Index(
            "idx_operator_escalation_idempotency_ticket_id",
            "ticket_id",
            postgresql_where=text("ticket_id IS NOT NULL"),
        ),
        {"schema": "mbb"},
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    actor_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_operator_escalation_idempotency_actor_account_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    reservation_token: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    locked_until: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    ticket_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.escalation_tickets.ticket_id",
            name="fk_operator_escalation_idempotency_ticket_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    response_status_code: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
