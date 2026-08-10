"""Compact, append-only persistence for finalized MBB AI turn provenance."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AITurnAudit(Base):
    __tablename__ = "ai_turn_audits"
    __table_args__ = (
        CheckConstraint("actor_type = 'ai'", name="chk_ai_turn_audits_actor_type"),
        CheckConstraint("actor_id = 'mbb_ai'", name="chk_ai_turn_audits_actor_id"),
        CheckConstraint(
            "actor_display_name = 'MBB AI Assistant'",
            name="chk_ai_turn_audits_actor_display_name",
        ),
        CheckConstraint(
            "outcome IN ('response_generated', 'fallback_used', "
            "'handoff_requested', 'failed', 'no_action')",
            name="chk_ai_turn_audits_outcome",
        ),
        CheckConstraint(
            "commercial_state_revision_before IS NULL OR "
            "commercial_state_revision_before >= 0",
            name="chk_ai_turn_audits_revision_before",
        ),
        CheckConstraint(
            "commercial_state_revision_after IS NULL OR "
            "commercial_state_revision_after >= 0",
            name="chk_ai_turn_audits_revision_after",
        ),
        CheckConstraint(
            "commercial_state_revision_before IS NULL OR "
            "commercial_state_revision_after IS NULL OR "
            "commercial_state_revision_after >= commercial_state_revision_before",
            name="chk_ai_turn_audits_revision_order",
        ),
        CheckConstraint(
            "jsonb_typeof(exposed_capabilities) = 'array' AND "
            "jsonb_array_length(exposed_capabilities) <= 16",
            name="chk_ai_turn_audits_exposed_capabilities",
        ),
        CheckConstraint(
            "jsonb_typeof(capability_activity) = 'array' AND "
            "jsonb_array_length(capability_activity) <= 16",
            name="chk_ai_turn_audits_capability_activity",
        ),
        CheckConstraint(
            "jsonb_typeof(commercial_state_changed_fields) = 'array' AND "
            "jsonb_array_length(commercial_state_changed_fields) <= 8",
            name="chk_ai_turn_audits_changed_fields",
        ),
        Index(
            "idx_ai_turn_audits_conversation_created",
            "conversation_id",
            "created_at",
        ),
        Index("idx_ai_turn_audits_source_message", "source_message_id"),
        Index("idx_ai_turn_audits_outbound_message", "outbound_message_id"),
        {"schema": "mbb"},
    )

    turn_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.conversations.conversation_id",
            name="fk_ai_turn_audits_conversation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    actor_type: Mapped[str] = mapped_column(String(10), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.messages.message_id",
            name="fk_ai_turn_audits_source_message_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.messages.message_id",
            name="fk_ai_turn_audits_outbound_message_id",
            ondelete="RESTRICT",
        ),
        nullable=True,
    )
    policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    exposed_capabilities: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    capability_activity: Mapped[list[dict]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    commercial_state_revision_before: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    commercial_state_revision_after: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    commercial_state_changed_fields: Mapped[list[str]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
    )
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    safe_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
    )
