"""Durable no-send Relance V2 candidate and confirmed-attempt evidence."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, TIMESTAMP, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RelanceCandidate(Base):
    __tablename__ = "relance_candidates"
    __table_args__ = (
        CheckConstraint("attempt_number BETWEEN 1 AND 2", name="chk_relance_candidate_attempt"),
        CheckConstraint(
            "cancelled_at IS NULL OR confirmed_sent_at IS NULL",
            name="chk_relance_candidate_terminal",
        ),
        Index(
            "uq_relance_candidate_episode_attempt",
            "lead_id", "source_message_id", "attempt_number", unique=True,
        ),
        Index(
            "uq_relance_candidate_active_lead", "lead_id", unique=True,
            postgresql_where=text("cancelled_at IS NULL AND confirmed_sent_at IS NULL"),
        ),
        {"schema": "mbb"},
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.leads.lead_id", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.messages.message_id", ondelete="CASCADE"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    cancelled_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    confirmed_sent_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
