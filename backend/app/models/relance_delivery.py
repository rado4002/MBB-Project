"""One durable outbound identity and outcome per Relance candidate."""

import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, ForeignKey, String, TIMESTAMP, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class RelanceDelivery(Base):
    __tablename__ = "relance_deliveries"
    __table_args__ = (
        CheckConstraint(
            "status IN ('prepared', 'sent', 'failed', 'uncertain', 'cancelled')",
            name="chk_relance_delivery_status",
        ),
        UniqueConstraint("outbound_message_id", name="uq_relance_delivery_message"),
        {"schema": "mbb"},
    )

    candidate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.relance_candidates.candidate_id", ondelete="CASCADE"),
        primary_key=True,
    )
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.messages.message_id", ondelete="RESTRICT")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(50))
    provider_message_id: Mapped[str | None] = mapped_column(String(100))
    dispatch_started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
