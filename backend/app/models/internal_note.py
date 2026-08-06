"""Immutable internal conversation notes — mbb.internal_notes."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, String, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InternalNote(Base):
    __tablename__ = "internal_notes"
    __table_args__ = (
        CheckConstraint(
            "char_length(content) BETWEEN 1 AND 4096",
            name="chk_internal_notes_content_length",
        ),
        CheckConstraint(
            "char_length(btrim(author_display_name)) BETWEEN 1 AND 100",
            name="chk_internal_notes_author_display_name",
        ),
        Index(
            "idx_internal_notes_conversation_created_note",
            "conversation_id",
            "created_at",
            "note_id",
        ),
        {"schema": "mbb"},
    )

    note_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.conversations.conversation_id",
            name="fk_internal_notes_conversation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    author_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_internal_notes_author_account_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    author_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
