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
        CheckConstraint(
            "owner_type IN ('ai', 'human')",
            name="chk_conv_owner_type",
        ),
        CheckConstraint(
            "ai_execution_state IN ('eligible', 'paused')",
            name="chk_conv_ai_execution_state",
        ),
        CheckConstraint(
            "(owner_type = 'ai' AND human_owner_account_id IS NULL "
            "AND ai_execution_state IN ('eligible', 'paused')) OR "
            "(owner_type = 'human' AND human_owner_account_id IS NOT NULL "
            "AND ai_execution_state = 'paused')",
            name="chk_conv_exclusive_owner",
        ),
        CheckConstraint(
            "ownership_version > 0",
            name="chk_conv_ownership_version_positive",
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
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    last_message_time: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
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
    owner_type: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="ai"
    )
    human_owner_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_conversations_human_owner_account_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    ai_execution_state: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="eligible"
    )
    ownership_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    ownership_updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
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
