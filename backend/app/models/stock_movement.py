"""Immutable evidence of an Administrator stock quantity operation."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StockMovement(Base):
    __tablename__ = "stock_movements"
    __table_args__ = (
        UniqueConstraint("sellable_item_id", "operation_key", name="uq_stock_movements_item_operation_key"),
        CheckConstraint("operation_kind IN ('receive', 'adjust', 'dispatch')", name="chk_stock_movements_kind"),
        CheckConstraint("before_quantity IS NULL OR before_quantity >= 0", name="chk_stock_movements_before"),
        CheckConstraint("after_quantity IS NULL OR after_quantity >= 0", name="chk_stock_movements_after"),
        CheckConstraint(
            "(operation_kind = 'receive' AND operation_key IS NOT NULL "
            "AND before_quantity IS NOT NULL AND received_amount IS NOT NULL "
            "AND received_amount > 0 AND after_quantity IS NOT NULL "
            "AND corrected_quantity IS NULL AND after_quantity = before_quantity + received_amount "
            "AND order_id IS NULL AND dispatched_quantity IS NULL "
            "AND handoff_reference IS NULL AND dispatch_request_fingerprint IS NULL) "
            "OR (operation_kind = 'adjust' AND received_amount IS NULL "
            "AND corrected_quantity IS NOT DISTINCT FROM after_quantity "
            "AND reason IS NOT NULL AND length(btrim(reason)) > 0 "
            "AND order_id IS NULL AND dispatched_quantity IS NULL "
            "AND handoff_reference IS NULL AND dispatch_request_fingerprint IS NULL) "
            "OR (operation_kind = 'dispatch' AND operation_key IS NOT NULL "
            "AND order_id IS NOT NULL AND before_quantity IS NOT NULL "
            "AND dispatched_quantity > 0 AND after_quantity IS NOT NULL "
            "AND after_quantity = before_quantity - dispatched_quantity "
            "AND received_amount IS NULL AND corrected_quantity IS NULL AND reason IS NULL "
            "AND handoff_reference IS NOT NULL AND length(btrim(handoff_reference)) > 0 "
            "AND dispatch_request_fingerprint IS NOT NULL "
            "AND dispatch_request_fingerprint ~ '^[0-9a-f]{64}$')",
            name="chk_stock_movements_shape",
        ),
        Index("idx_stock_movements_item_occurred", "sellable_item_id", "occurred_at", "movement_id"),
        Index(
            "uq_stock_movements_dispatch_order",
            "order_id",
            unique=True,
            postgresql_where=text("operation_kind = 'dispatch'"),
        ),
        Index(
            "uq_stock_movements_dispatch_operation_key",
            "operation_key",
            unique=True,
            postgresql_where=text("operation_kind = 'dispatch'"),
        ),
        {"schema": "mbb"},
    )

    movement_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sellable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.sellable_items.sellable_item_id", ondelete="RESTRICT"), nullable=False
    )
    operation_kind: Mapped[str] = mapped_column(String(10), nullable=False)
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.stock_allocations.order_id", ondelete="RESTRICT"),
        nullable=True,
    )
    before_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    before_updated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    received_amount: Mapped[int | None] = mapped_column(Integer, nullable=True)
    corrected_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    dispatched_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    after_quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    handoff_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)
    dispatch_request_fingerprint: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    actor_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.operator_accounts.account_id", ondelete="RESTRICT"), nullable=False
    )
    operation_key: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
