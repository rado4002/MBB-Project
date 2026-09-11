"""Server-owned, non-consequential confirmable order drafts."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class OrderDraft(Base):
    """One immutable commercial snapshot in a versioned draft chain."""

    __tablename__ = "order_drafts"
    __table_args__ = (
        CheckConstraint("draft_version > 0", name="chk_order_drafts_version"),
        CheckConstraint(
            "status IN ('awaiting_confirmation', 'confirmed', 'cancelled', "
            "'invalidated')",
            name="chk_order_drafts_status",
        ),
        CheckConstraint(
            "quantity BETWEEN 1 AND 1000",
            name="chk_order_drafts_quantity",
        ),
        CheckConstraint(
            "unit_price_usd > 0 AND unit_price_cdf > 0 AND total_cdf > 0",
            name="chk_order_drafts_amounts",
        ),
        CheckConstraint(
            "total_cdf = unit_price_cdf * quantity",
            name="chk_order_drafts_total",
        ),
        CheckConstraint(
            "ownership_version > 0 AND commercial_state_revision >= 0",
            name="chk_order_drafts_authority",
        ),
        CheckConstraint(
            "confirmation_code ~ '^[A-F0-9]{8}$'",
            name="chk_order_drafts_confirmation_code",
        ),
        CheckConstraint(
            "char_length(offer_fingerprint) = 64",
            name="chk_order_drafts_offer_fingerprint",
        ),
        CheckConstraint(
            "(status = 'awaiting_confirmation' AND resolved_by_message_id IS NULL "
            "AND resolution_outbound_message_id IS NULL AND resolved_at IS NULL "
            "AND resolution_code IS NULL) OR "
            "(status <> 'awaiting_confirmation' AND resolved_by_message_id IS NOT NULL "
            "AND resolution_outbound_message_id IS NOT NULL AND resolved_at IS NOT NULL "
            "AND resolution_code IS NOT NULL)",
            name="chk_order_drafts_resolution",
        ),
        Index(
            "uq_order_drafts_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text("status = 'awaiting_confirmation'"),
        ),
        Index(
            "uq_order_drafts_confirmation_code",
            "confirmation_code",
            unique=True,
        ),
        Index(
            "uq_order_drafts_source_message",
            "source_message_id",
            unique=True,
        ),
        Index(
            "uq_order_drafts_resolved_message",
            "resolved_by_message_id",
            unique=True,
            postgresql_where=text("resolved_by_message_id IS NOT NULL"),
        ),
        Index(
            "idx_order_drafts_conversation_created",
            "conversation_id",
            "created_at",
        ),
        ForeignKeyConstraint(
            ["presented_outbound_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_presented_outbound_message_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["resolved_by_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_resolved_by_message_id",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["resolution_outbound_message_id"],
            ["mbb.messages.message_id"],
            name="fk_order_drafts_resolution_outbound_message_id",
            ondelete="RESTRICT",
        ),
        {"schema": "mbb"},
    )

    draft_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    draft_version: Mapped[int] = mapped_column(Integer, primary_key=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.conversations.conversation_id",
            name="fk_order_drafts_conversation_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.messages.message_id",
            name="fk_order_drafts_source_message_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    presented_outbound_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    resolved_by_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    resolution_outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True
    )
    created_by_turn_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    ownership_version: Mapped[int] = mapped_column(Integer, nullable=False)
    commercial_state_revision: Mapped[int] = mapped_column(Integer, nullable=False)

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.products.product_id",
            name="fk_order_drafts_product_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    sellable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.sellable_items.sellable_item_id",
            name="fk_order_drafts_sellable_item_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    product_name: Mapped[str] = mapped_column(String(200), nullable=False)
    model_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)

    price_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.sellable_item_prices.price_id",
            name="fk_order_drafts_price_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    unit_price_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    price_effective_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    exchange_rate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.exchange_rates.exchange_rate_id",
            name="fk_order_drafts_exchange_rate_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    unit_price_cdf: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    total_cdf: Mapped[Decimal] = mapped_column(Numeric(24, 2), nullable=False)
    exchange_rate_effective_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    inventory_status: Mapped[str] = mapped_column(String(20), nullable=False)
    inventory_updated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    offer_read_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False
    )
    offer_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)

    confirmation_code: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="awaiting_confirmation"
    )
    resolution_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
