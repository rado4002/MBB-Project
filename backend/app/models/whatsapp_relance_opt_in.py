"""Operator-verified WhatsApp Relance opt-in evidence."""

import uuid
from datetime import datetime

from sqlalchemy import ForeignKey, TIMESTAMP, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class WhatsAppRelanceOptIn(Base):
    __tablename__ = "whatsapp_relance_opt_ins"
    __table_args__ = (
        UniqueConstraint("source_message_id", name="uq_whatsapp_relance_opt_in_message"),
        {"schema": "mbb"},
    )

    opt_in_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    customer_id: Mapped[str] = mapped_column(
        ForeignKey("mbb.customers.phone_number", ondelete="CASCADE"), nullable=False
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("mbb.messages.message_id", ondelete="RESTRICT"), nullable=False
    )
    verified_by_operator_account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.operator_accounts.account_id", ondelete="RESTRICT"),
        nullable=False,
    )
    granted_at: Mapped[datetime] = mapped_column(TIMESTAMP(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
