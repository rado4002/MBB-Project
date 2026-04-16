"""Order ORM model — mbb.orders"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    TIMESTAMP,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Order(Base):
    __tablename__ = "orders"
    __table_args__ = (
        CheckConstraint(
            "payment_type IN ('mobile_money', 'bank_transfer', 'cod')",
            name="chk_order_payment",
        ),
        CheckConstraint(
            "status IN ('pending', 'confirmed', 'preparing', 'delivering', 'delivered', 'cancelled')",
            name="chk_order_status",
        ),
        CheckConstraint(
            "delivery_method IN ('moto_taxi', 'spot_pickup')",
            name="chk_order_delivery",
        ),
        CheckConstraint("total_amount > 0", name="chk_order_amount"),
        Index("idx_orders_lead", "lead_id"),
        Index("idx_orders_customer", "customer_id"),
        Index("idx_orders_status", "status"),
        Index("idx_orders_created", "created_at"),
        Index(
            "idx_orders_hub_sync",
            "hub_crm_synced",
            postgresql_where=text("hub_crm_synced = FALSE"),
        ),
        {"schema": "mbb"},
    )

    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    lead_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.leads.lead_id", ondelete="RESTRICT"),
        nullable=False,
    )
    customer_id: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("mbb.customers.phone_number", ondelete="RESTRICT"),
        nullable=False,
    )
    items: Mapped[list] = mapped_column(JSONB, nullable=False)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default="CDF"
    )
    payment_type: Mapped[str] = mapped_column(String(20), nullable=False)
    delivery_zone: Mapped[str] = mapped_column(String(50), nullable=False)
    delivery_method: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="moto_taxi"
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    hub_crm_synced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    hub_crm_order_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    club_points_credited: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )

    # Relationships
    lead: Mapped["Lead"] = relationship(back_populates="orders")  # noqa: F821
    customer: Mapped["Customer"] = relationship(back_populates="orders")  # noqa: F821
    payments: Mapped[list["Payment"]] = relationship(  # noqa: F821
        back_populates="order", cascade="all, delete-orphan"
    )
