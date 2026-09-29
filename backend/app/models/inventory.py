"""Sellable item stock quantity for one MBB stock pool."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class InventoryRecord(Base):
    __tablename__ = "inventory_statuses"
    __table_args__ = (
        UniqueConstraint(
            "sellable_item_id", name="uq_inventory_statuses_sellable_item_id"
        ),
        CheckConstraint(
            "status IN ('available', 'out_of_stock', 'unknown')",
            name="chk_inventory_statuses_status",
        ),
        CheckConstraint("quantity IS NULL OR quantity >= 0", name="chk_inventory_statuses_quantity"),
        {"schema": "mbb"},
    )

    inventory_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sellable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.sellable_items.sellable_item_id",
            name="fk_inventory_statuses_sellable_item_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    quantity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
