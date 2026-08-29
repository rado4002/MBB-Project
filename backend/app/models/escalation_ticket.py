"""EscalationTicket ORM model — mbb.escalation_tickets"""
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class EscalationTicket(Base):
    __tablename__ = "escalation_tickets"
    __table_args__ = (
        CheckConstraint(
            "priority IN ('high', 'medium', 'low')", name="chk_esc_priority"
        ),
        CheckConstraint(
            "reason IN ('voice_note', 'complex_complaint', 'high_value_lead', "
            "'unresolved_3x', 'sav_issue', 'human_handoff', "
            "'qualified_purchase_intent', 'explicit_human_request', "
            "'authority_required', 'reliability_tool_failure')",
            name="chk_esc_reason",
        ),
        CheckConstraint(
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name="chk_esc_status",
        ),
        CheckConstraint(
            "source IN ('legacy', 'operator_browser', 'ai_capability')",
            name="chk_esc_source",
        ),
        CheckConstraint(
            "escalation_type IS NULL OR escalation_type IN "
            "('voice_note', 'complex_issue', 'high_value_lead', 'payment_issue', "
            "'human_handoff')",
            name="chk_esc_escalation_type",
        ),
        CheckConstraint(
            "operator_reason IS NULL OR "
            "char_length(btrim(operator_reason)) BETWEEN 10 AND 500",
            name="chk_esc_operator_reason",
        ),
        CheckConstraint(
            "source <> 'operator_browser' OR "
            "(escalation_type IS NOT NULL AND operator_reason IS NOT NULL "
            "AND created_by_account_id IS NOT NULL)",
            name="chk_esc_operator_browser_fields",
        ),
        CheckConstraint(
            "source <> 'ai_capability' OR "
            "(escalation_type = 'human_handoff' AND reason IN "
            "('human_handoff', 'qualified_purchase_intent', "
            "'explicit_human_request', 'authority_required', "
            "'reliability_tool_failure') "
            "AND operator_reason IS NULL AND created_by_account_id IS NULL)",
            name="chk_esc_ai_capability_fields",
        ),
        Index("idx_esc_conversation", "conversation_id"),
        Index("idx_esc_customer", "customer_id"),
        Index(
            "idx_esc_status",
            "status",
            postgresql_where=text("status IN ('open', 'in_progress')"),
        ),
        Index("idx_esc_priority", "priority", "created_at"),
        Index(
            "idx_esc_created_by_account",
            "created_by_account_id",
            "created_at",
            postgresql_where=text("created_by_account_id IS NOT NULL"),
        ),
        Index(
            "uq_escalation_tickets_one_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status IN ('open', 'in_progress')"),
        ),
        {"schema": "mbb"},
    )

    ticket_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.leads.lead_id", ondelete="SET NULL"),
        nullable=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"),
        nullable=False,
    )
    priority: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="medium"
    )
    reason: Mapped[str] = mapped_column(String(50), nullable=False)
    source: Mapped[str] = mapped_column(
        String(30), nullable=False, server_default="legacy"
    )
    escalation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    operator_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_escalation_tickets_created_by_account_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="open"
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    maps_tags_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    lead: Mapped["Lead | None"] = relationship(back_populates="escalations")  # noqa: F821
    conversation: Mapped["Conversation"] = relationship(back_populates="escalations")  # noqa: F821
    customer: Mapped["Customer"] = relationship(back_populates="escalations")  # noqa: F821
