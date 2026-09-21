"""Durable completion state for one accepted inbound M1 turn."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InboundTurnLifecycle(Base):
    """Small recovery record anchored to the authoritative inbound message."""

    __tablename__ = "inbound_turn_lifecycles"
    __table_args__ = (
        CheckConstraint(
            "state IN ('pending', 'processing', 'outcome_committed', "
            "'send_completed', 'send_uncertain', 'skipped')",
            name="chk_inbound_turn_lifecycles_state",
        ),
        CheckConstraint(
            "outcome_type IS NULL OR outcome_type IN "
            "('response', 'fallback', 'order_draft', 'handoff', "
            "'draft_reply', 'opt_out', 'voice_note')",
            name="chk_inbound_turn_lifecycles_outcome_type",
        ),
        CheckConstraint(
            "ownership_version > 0",
            name="chk_inbound_turn_lifecycles_ownership_version",
        ),
        CheckConstraint(
            "(outbound_message_id IS NULL AND outcome_type IS NULL) OR "
            "(outbound_message_id IS NOT NULL AND outcome_type IS NOT NULL)",
            name="chk_inbound_turn_lifecycles_outcome_pair",
        ),
        CheckConstraint(
            "state NOT IN ('outcome_committed', 'send_completed', 'send_uncertain') "
            "OR outbound_message_id IS NOT NULL",
            name="chk_inbound_turn_lifecycles_outcome_required",
        ),
        CheckConstraint(
            "char_length(attempt_id) BETWEEN 1 AND 64 OR attempt_id IS NULL",
            name="chk_inbound_turn_lifecycles_attempt_id",
        ),
        CheckConstraint(
            "char_length(disposition_code) BETWEEN 1 AND 64 "
            "OR disposition_code IS NULL",
            name="chk_inbound_turn_lifecycles_disposition_code",
        ),
        Index("idx_inbound_turn_lifecycles_state_updated", "state", "updated_at"),
        Index("idx_inbound_turn_lifecycles_outbound", "outbound_message_id"),
        {"schema": "mbb"},
    )

    source_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.messages.message_id",
            name="fk_inbound_turn_lifecycles_source_message_id",
            ondelete="CASCADE",
        ),
        primary_key=True,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.conversations.conversation_id",
            name="fk_inbound_turn_lifecycles_conversation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    ownership_version: Mapped[int] = mapped_column(Integer, nullable=False)
    state: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="pending"
    )
    outcome_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.messages.message_id",
            name="fk_inbound_turn_lifecycles_outbound_message_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    disposition_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attempt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
