"""Broader operator audit persistence, separate from legacy admin audit."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class OperatorAuditEvent(Base):
    __tablename__ = "operator_audit_events"
    __table_args__ = (
        CheckConstraint(
            "category IN ('business', 'security')",
            name="chk_operator_audit_events_category",
        ),
        CheckConstraint(
            "actor_kind IN ('human', 'service', 'bootstrap', 'unknown')",
            name="chk_operator_audit_events_actor_kind",
        ),
        CheckConstraint(
            "outcome IN ('succeeded', 'denied', 'failed')",
            name="chk_operator_audit_events_outcome",
        ),
        CheckConstraint(
            "effective_role IS NULL OR effective_role IN ('administrator', 'operator', 'analyst')",
            name="chk_operator_audit_events_effective_role",
        ),
        CheckConstraint(
            "retain_until > occurred_at",
            name="chk_operator_audit_events_retention",
        ),
        Index("idx_operator_audit_events_occurred_at", "occurred_at"),
        Index("idx_operator_audit_events_retain_until", "retain_until"),
        Index(
            "idx_operator_audit_events_category_occurred",
            "category",
            "occurred_at",
        ),
        Index(
            "idx_operator_audit_events_actor_occurred",
            "actor_account_id",
            "occurred_at",
        ),
        Index("idx_operator_audit_events_action", "action"),
        Index("idx_operator_audit_events_request_id", "request_id"),
        {"schema": "mbb"},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    category: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_operator_audit_events_actor_account_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    actor_display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    effective_role: Mapped[str | None] = mapped_column(String(20), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    target_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_reference: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    event_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    retain_until: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    security_metadata: Mapped["OperatorAuditSecurityMetadata | None"] = relationship(
        back_populates="event", uselist=False
    )


class OperatorAuditSecurityMetadata(Base):
    __tablename__ = "operator_audit_security_metadata"
    __table_args__ = (
        CheckConstraint(
            "source_network_fingerprint IS NULL OR source_network_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_operator_audit_security_metadata_network_fingerprint",
        ),
        CheckConstraint(
            "user_agent_fingerprint IS NULL OR user_agent_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_operator_audit_security_metadata_user_agent_fingerprint",
        ),
        Index(
            "idx_operator_audit_security_metadata_retain_until", "retain_until"
        ),
        {"schema": "mbb"},
    )

    event_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_audit_events.event_id",
            name="fk_operator_audit_security_metadata_event_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    source_network_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    user_agent_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    retain_until: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )

    event: Mapped[OperatorAuditEvent] = relationship(back_populates="security_metadata")
