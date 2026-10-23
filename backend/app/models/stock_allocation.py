"""Immutable full-quantity Stock allocation for one Order."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Integer, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class StockAllocation(Base):
    __tablename__ = "stock_allocations"
    __table_args__ = (
        CheckConstraint("quantity > 0", name="chk_stock_allocations_quantity"),
        CheckConstraint(
            "before_available_quantity >= quantity "
            "AND after_available_quantity >= 0 "
            "AND after_available_quantity = before_available_quantity - quantity",
            name="chk_stock_allocations_balance",
        ),
        Index(
            "idx_stock_allocations_item_allocated",
            "sellable_item_id",
            "allocated_at",
            "order_id",
        ),
        {"schema": "mbb"},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.orders.order_id", ondelete="RESTRICT"),
        primary_key=True,
    )
    sellable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.sellable_items.sellable_item_id", ondelete="RESTRICT"),
        nullable=False,
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    before_available_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    after_available_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    allocated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
