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
            "status IN ('open', 'in_progress', 'resolved', 'closed')",
            name="chk_esc_status",
        ),
        Index("idx_esc_conversation", "conversation_id"),
        Index("idx_esc_customer", "customer_id"),
        Index(
            "idx_esc_status",
            "status",
            postgresql_where=text("status IN ('open', 'in_progress')"),
        ),
        Index("idx_esc_priority", "priority", "created_at"),
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
    assigned_to: Mapped[str | None] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="open"
    )
    resolution_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    transcript_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    maps_tags_snapshot: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    assigned_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    lead: Mapped["Lead | None"] = relationship(back_populates="escalations")  # noqa: F821
    conversation: Mapped["Conversation"] = relationship(back_populates="escalations")  # noqa: F821
    customer: Mapped["Customer"] = relationship(back_populates="escalations")  # noqa: F821
