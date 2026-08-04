"""Durable retry ledger for browser conversation ownership transitions."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class ConversationOwnershipIdempotency(Base):
    __tablename__ = "conversation_ownership_idempotency"
    __table_args__ = (
        UniqueConstraint(
            "actor_account_id",
            "key_digest",
            name="uq_conversation_ownership_idempotency_actor_key",
        ),
        CheckConstraint(
            "state IN ('in_progress', 'completed')",
            name="chk_conversation_ownership_idempotency_state",
        ),
        CheckConstraint(
            "target_owner_type IN ('ai', 'human')",
            name="chk_conversation_ownership_idempotency_target",
        ),
        CheckConstraint(
            "expected_version > 0 AND (result_version IS NULL OR result_version > 0)",
            name="chk_conversation_ownership_idempotency_versions",
        ),
        Index("idx_conversation_ownership_idempotency_created_at", "created_at"),
        {"schema": "mbb"},
    )

    record_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    actor_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.operator_accounts.account_id",
            name="fk_conversation_ownership_idempotency_actor_account_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.conversations.conversation_id",
            name="fk_conversation_ownership_idempotency_conversation_id",
            ondelete="CASCADE",
        ),
        nullable=False,
    )
    key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False)
    reservation_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    locked_until: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    target_owner_type: Mapped[str] = mapped_column(String(10), nullable=False)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_version: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
