"""MapsTag ORM model — mbb.maps_tags"""
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, String, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class MapsTag(Base):
    __tablename__ = "maps_tags"
    __table_args__ = (
        CheckConstraint(
            "category IN ('product_demand', 'silence_reason', 'conversion_trigger', 'language_usage', 'opt_out_reason')",
            name="chk_maps_category",
        ),
        Index("idx_maps_message", "message_id"),
        Index("idx_maps_category", "category"),
        Index("idx_maps_pattern", "pattern"),
        Index("idx_maps_created", "created_at"),
        Index("idx_maps_city_category", "city", "category"),
        Index("idx_maps_metadata", "metadata", postgresql_using="gin"),
        {"schema": "mbb"},
    )

    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("mbb.messages.message_id", ondelete="CASCADE"),
        nullable=False,
    )
    # Denormalized for fast aggregation (no JOIN needed on dashboard queries)
    conversation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    customer_id: Mapped[str] = mapped_column(String(20), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    pattern: Mapped[str] = mapped_column(String(200), nullable=False)
    trigger_event: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str | None] = mapped_column(String(50), nullable=True)
    language: Mapped[str | None] = mapped_column(String(10), nullable=True)
    tag_metadata: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )

    # Relationships
    message: Mapped["Message"] = relationship(back_populates="maps_tags")  # noqa: F821
