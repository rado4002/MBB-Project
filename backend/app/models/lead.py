"""Lead ORM model — mbb.leads"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    TIMESTAMP,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Lead(Base):
    __tablename__ = "leads"
    __table_args__ = (
        CheckConstraint(
            "score IN ('hot', 'warm', 'cold')", name="chk_lead_score"
        ),
        CheckConstraint(
            "stage IN ('awareness', 'consideration', 'decision')",
            name="chk_lead_stage",
        ),
        CheckConstraint(
            "relance_count BETWEEN 0 AND 3", name="chk_lead_relance"
        ),
        CheckConstraint(
            "score_value BETWEEN 0 AND 10", name="chk_score_value"
        ),
        UniqueConstraint("conversation_id", name="uq_lead_conversation"),
        Index("idx_leads_customer", "customer_id"),
        Index("idx_leads_score", "score"),
        Index("idx_leads_stage", "stage"),
        Index(
            "idx_leads_relance",
            "relance_count",
            postgresql_where=text("relance_count < 3"),
        ),
        Index("idx_leads_source", "source"),
        Index("idx_leads_qualified", "qualified_at"),
        {"schema": "mbb"},
    )

    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    customer_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    score: Mapped[str] = mapped_column(String(10), nullable=False)
    score_value: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    stage: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="awareness"
    )
    intent: Mapped[str] = mapped_column(String(30), nullable=False)
    product_interest: Mapped[list[str]] = mapped_column(
        ARRAY(String), nullable=False, server_default=text("'{}'")
    )
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    relance_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    qualified_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    last_nurture_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    converted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(  # noqa: F821
        back_populates="leads"
    )
    conversation: Mapped["Conversation"] = relationship(  # noqa: F821
        back_populates="lead"
    )
    relances: Mapped[list["Relance"]] = relationship(  # noqa: F821
        back_populates="lead", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(  # noqa: F821
        back_populates="lead"
    )
    escalations: Mapped[list["EscalationTicket"]] = relationship(  # noqa: F821
        back_populates="lead"
    )
