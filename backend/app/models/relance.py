"""Relance ORM model — mbb.relances"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Relance(Base):
    __tablename__ = "relances"
    __table_args__ = (
        CheckConstraint(
            "attempt_number BETWEEN 1 AND 3", name="chk_relance_attempt"
        ),
        CheckConstraint(
            "hook_type IN ('reciprocity', 'social_proof', 'scarcity', 'loyalty')",
            name="chk_relance_hook_type",
        ),
        UniqueConstraint("lead_id", "attempt_number", name="uq_relance_lead_attempt"),
        Index("idx_relance_lead", "lead_id"),
        Index(
            "idx_relance_scheduled",
            "scheduled_at",
            postgresql_where=text("delivered_at IS NULL AND cancelled = FALSE"),
        ),
        Index("idx_relance_performance", "attempt_number", "response_received"),
        {"schema": "mbb"},
    )

    relance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.leads.lead_id", ondelete="CASCADE"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    value_hook: Mapped[str] = mapped_column(Text, nullable=False)
    hook_type: Mapped[str] = mapped_column(
        String(30),
        nullable=False
    )
    response_received: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    response_time_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancelled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # Relationships
    lead: Mapped["Lead"] = relationship(back_populates="relances")  # noqa: F821
