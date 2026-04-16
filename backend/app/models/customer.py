"""Customer ORM model — mbb.customers"""
from datetime import datetime

from sqlalchemy import TIMESTAMP, Boolean, CheckConstraint, Index, Integer, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class Customer(Base):
    __tablename__ = "customers"
    __table_args__ = (
        CheckConstraint(
            "preferred_language IN ('lingala', 'french', 'swahili')",
            name="chk_language",
        ),
        CheckConstraint(
            r"phone_number ~ '^\+243[0-9]{9}$'",
            name="chk_phone_format",
        ),
        CheckConstraint("club_points >= 0", name="chk_club_points"),
        Index("idx_customers_city", "city"),
        Index(
            "idx_customers_opt_out",
            "opt_out_flag",
            postgresql_where=text("opt_out_flag = TRUE"),
        ),
        Index("idx_customers_last_interaction", "last_interaction"),
        Index(
            "idx_customers_club",
            "club_member",
            postgresql_where=text("club_member = TRUE"),
        ),
        {"schema": "mbb"},
    )

    phone_number: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    city: Mapped[str] = mapped_column(String(50), nullable=False)
    preferred_language: Mapped[str] = mapped_column(
        String(10), nullable=False, server_default="french"
    )
    opt_out_flag: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    opt_out_at: Mapped[datetime | None] = mapped_column(TIMESTAMP(timezone=True), nullable=True)
    consent_given: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    consent_timestamp: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    club_member: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    club_points: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    created_at: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )
    last_interaction: Mapped[datetime] = mapped_column(
        nullable=False, server_default=text("NOW()")
    )

    # Relationships
    conversations: Mapped[list["Conversation"]] = relationship(  # noqa: F821
        back_populates="customer", cascade="all, delete-orphan"
    )
    leads: Mapped[list["Lead"]] = relationship(  # noqa: F821
        back_populates="customer", cascade="all, delete-orphan"
    )
    orders: Mapped[list["Order"]] = relationship(  # noqa: F821
        back_populates="customer"
    )
    escalations: Mapped[list["EscalationTicket"]] = relationship(  # noqa: F821
        back_populates="customer", cascade="all, delete-orphan"
    )
