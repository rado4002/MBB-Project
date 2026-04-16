"""Conversation ORM model — mbb.conversations"""
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'qualifying', 'nurturing', 'escalated', 'converted', 'dormant')",
            name="chk_conv_status",
        ),
        CheckConstraint(
            "language_detected IN ('lingala', 'french', 'swahili')",
            name="chk_conv_language",
        ),
        Index("idx_conv_customer", "customer_id"),
        Index("idx_conv_status", "status"),
        Index("idx_conv_last_msg", "last_message_time"),
        Index("idx_conv_context", "context", postgresql_using="gin"),
        {"schema": "mbb"},
    )

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    customer_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"),
        nullable=False,
    )
    start_time: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    last_message_time: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active"
    )
    language_detected: Mapped[str] = mapped_column(String(10), nullable=False)
    context: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    message_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )

    # Relationships
    customer: Mapped["Customer"] = relationship(  # noqa: F821
        back_populates="conversations"
    )
    messages: Mapped[list["Message"]] = relationship(  # noqa: F821
        back_populates="conversation", cascade="all, delete-orphan"
    )
    lead: Mapped["Lead | None"] = relationship(  # noqa: F821
        back_populates="conversation", uselist=False
    )
    escalations: Mapped[list["EscalationTicket"]] = relationship(  # noqa: F821
        back_populates="conversation", cascade="all, delete-orphan"
    )
