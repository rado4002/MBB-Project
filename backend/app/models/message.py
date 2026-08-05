"""Message ORM model — mbb.messages"""
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint(
            "direction IN ('inbound', 'outbound')", name="chk_msg_direction"
        ),
        CheckConstraint(
            "content_type IN ('text', 'voice_note', 'image')",
            name="chk_msg_content_type",
        ),
        CheckConstraint(
            "delivery_state IS NULL OR delivery_state IN "
            "('accepted', 'sent', 'failed', 'uncertain')",
            name="chk_msg_delivery_state",
        ),
        CheckConstraint(
            "(operator_author_account_id IS NULL AND author_display_name IS NULL "
            "AND accepted_ownership_version IS NULL AND delivery_state IS NULL "
            "AND delivery_state_timestamp IS NULL) OR "
            "(operator_author_account_id IS NOT NULL "
            "AND char_length(btrim(author_display_name)) BETWEEN 1 AND 100 "
            "AND accepted_ownership_version > 0 "
            "AND ((delivery_state IS NULL AND delivery_state_timestamp IS NULL) "
            "OR (delivery_state IS NOT NULL "
            "AND delivery_state_timestamp IS NOT NULL)) "
            "AND direction = 'outbound' AND content_type = 'text')",
            name="chk_msg_operator_authorship",
        ),
        Index("idx_msg_conversation", "conversation_id"),
        Index("idx_msg_timestamp", "timestamp"),
        Index("idx_msg_direction", "direction"),
        Index(
            "uq_messages_inbound_whatsapp_message_id",
            "whatsapp_message_id",
            unique=True,
            postgresql_where=text(
                "direction = 'inbound' AND whatsapp_message_id IS NOT NULL "
                "AND btrim(whatsapp_message_id) <> ''"
            ),
        ),
        Index(
            "idx_msg_content_type",
            "content_type",
            postgresql_where=text("content_type != 'text'"),
        ),
        {"schema": "mbb"},
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.conversations.conversation_id", ondelete="CASCADE"),
        nullable=False,
    )
    timestamp: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    direction: Mapped[str] = mapped_column(String(10), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="text"
    )
    language: Mapped[str] = mapped_column(String(10), nullable=False)
    persuasion_hook: Mapped[str | None] = mapped_column(String(50), nullable=True)
    processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    whatsapp_message_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    operator_author_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_messages_operator_author_account_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    author_display_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    accepted_ownership_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    delivery_state: Mapped[str | None] = mapped_column(String(20), nullable=True)
    delivery_state_timestamp: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # Relationships
    conversation: Mapped["Conversation"] = relationship(  # noqa: F821
        back_populates="messages"
    )
    maps_tags: Mapped[list["MapsTag"]] = relationship(  # noqa: F821
        back_populates="message", cascade="all, delete-orphan"
    )
