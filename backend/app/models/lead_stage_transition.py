"""Lead Stage Transition ORM model — mbb.lead_stage_transitions

Audit log for StoryBrand funnel stage changes (AWARENESS → CONSIDERATION → DECISION).
"""
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class LeadStageTransition(Base):
    __tablename__ = "lead_stage_transitions"
    __table_args__ = (
        CheckConstraint(
            "from_stage IN ('awareness', 'consideration', 'decision')",
            name="chk_transition_from_stage",
        ),
        CheckConstraint(
            "to_stage IN ('awareness', 'consideration', 'decision')",
            name="chk_transition_to_stage",
        ),
        Index("idx_stage_transitions_lead", "lead_id"),
        Index("idx_stage_transitions_created", "created_at"),
        {"schema": "mbb"},
    )

    transition_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.leads.lead_id", ondelete="CASCADE"),
        nullable=False,
    )
    from_stage: Mapped[str] = mapped_column(String(20), nullable=False)
    to_stage: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # Relationships
    lead: Mapped["Lead"] = relationship(  # noqa: F821
        back_populates="stage_transitions"
    )
