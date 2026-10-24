"""Durable customer-receipt evidence for one delivered Order."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    ForeignKey,
    Index,
    String,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrderDeliveryConfirmation(Base):
    __tablename__ = "order_delivery_confirmations"
    __table_args__ = (
        CheckConstraint(
            "evidence_inbound_receipt_order > 0",
            name="chk_order_delivery_confirmation_receipt_order",
        ),
        CheckConstraint(
            "evidence_received_at > dispatch_occurred_at",
            name="chk_order_delivery_confirmation_post_dispatch",
        ),
        CheckConstraint(
            "confirmed_at >= evidence_received_at",
            name="chk_order_delivery_confirmation_timestamps",
        ),
        CheckConstraint(
            "operation_key_digest ~ '^[0-9a-f]{64}$' "
            "AND request_fingerprint ~ '^[0-9a-f]{64}$' "
            "AND evidence_fingerprint ~ '^[0-9a-f]{64}$'",
            name="chk_order_delivery_confirmation_fingerprints",
        ),
        Index(
            "uq_order_delivery_confirmation_evidence_message",
            "evidence_message_id",
            unique=True,
        ),
        Index(
            "uq_order_delivery_confirmation_dispatch_movement",
            "dispatch_movement_id",
            unique=True,
        ),
        Index(
            "uq_order_delivery_confirmation_operation_key",
            "operation_key_digest",
            unique=True,
        ),
        {"schema": "mbb"},
    )

    confirmation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.orders.order_id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    evidence_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.messages.message_id", ondelete="RESTRICT"),
        nullable=False,
    )
    evidence_conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.conversations.conversation_id", ondelete="RESTRICT"),
        nullable=False,
    )
    dispatch_movement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.stock_movements.movement_id", ondelete="RESTRICT"),
        nullable=False,
    )
    evidence_inbound_receipt_order: Mapped[int] = mapped_column(
        BigInteger, nullable=False
    )
    evidence_received_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    dispatch_occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    evidence_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    actor_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.operator_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    operation_key_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
