"""Payment ORM model — mbb.payments"""
import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Numeric, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint(
            "method IN ('orange_money', 'airtel_money', 'mpesa', 'bank_transfer', 'cash')",
            name="chk_payment_method",
        ),
        CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'refunded')",
            name="chk_payment_status",
        ),
        CheckConstraint("amount > 0", name="chk_payment_amount"),
        Index("idx_payments_order", "order_id"),
        Index("idx_payments_status", "status"),
        Index(
            "idx_payments_provider",
            "provider_transaction_id",
            postgresql_where=text("provider_transaction_id IS NOT NULL"),
        ),
        {"schema": "mbb"},
    )

    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    order_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.orders.order_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # One payment per order
    )
    method: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(
        String(3), nullable=False, server_default="CDF"
    )
    provider_transaction_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    provider_response: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="pending"
    )
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    completed_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)

    # Relationships
    order: Mapped["Order"] = relationship(back_populates="payments")  # noqa: F821
