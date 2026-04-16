"""AdminAuditLog ORM model — mbb.admin_audit_log (append-only)"""
import uuid
from datetime import datetime

from sqlalchemy import TIMESTAMP, CheckConstraint, Index, String, Text, text
from sqlalchemy.dialects.postgresql import INET, JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_log"
    __table_args__ = (
        CheckConstraint(
            "user_role IN ('admin', 'hub', 'lab')", name="chk_audit_role"
        ),
        Index("idx_audit_user", "user_name"),
        Index("idx_audit_action", "action"),
        Index("idx_audit_target", "target_entity", "target_id"),
        Index("idx_audit_created", "created_at"),
        {"schema": "mbb"},
    )

    audit_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    user_name: Mapped[str] = mapped_column(String(50), nullable=False)
    user_role: Mapped[str] = mapped_column(String(10), nullable=False)
    action: Mapped[str] = mapped_column(String(50), nullable=False)
    target_entity: Mapped[str] = mapped_column(String(50), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    old_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    new_value: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    justification: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
