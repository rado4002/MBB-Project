"""Authoritative USD price and Administrator-maintained FX persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    TIMESTAMP,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class SellableItemPrice(Base):
    __tablename__ = "sellable_item_prices"
    __table_args__ = (
        CheckConstraint("amount > 0", name="chk_sellable_item_prices_amount_positive"),
        CheckConstraint(
            "currency ~ '^[A-Z]{3}$'",
            name="chk_sellable_item_prices_currency",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= effective_at",
            name="chk_sellable_item_prices_lifecycle",
        ),
        Index(
            "uq_sellable_item_prices_current_currency",
            "sellable_item_id",
            "currency",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "idx_sellable_item_prices_history",
            "sellable_item_id",
            "currency",
            "effective_at",
        ),
        {"schema": "mbb"},
    )

    price_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    sellable_item_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey(
            "mbb.sellable_items.sellable_item_id",
            name="fk_sellable_item_prices_sellable_item_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class ExchangeRate(Base):
    __tablename__ = "exchange_rates"
    __table_args__ = (
        CheckConstraint("rate > 0", name="chk_exchange_rates_rate_positive"),
        CheckConstraint(
            "base_currency ~ '^[A-Z]{3}$' AND quote_currency ~ '^[A-Z]{3}$'",
            name="chk_exchange_rates_currency_format",
        ),
        CheckConstraint(
            "base_currency <> quote_currency",
            name="chk_exchange_rates_distinct_currencies",
        ),
        CheckConstraint(
            "base_currency = 'USD' AND quote_currency = 'CDF'",
            name="chk_exchange_rates_supported_pair",
        ),
        CheckConstraint(
            "ended_at IS NULL OR ended_at >= effective_at",
            name="chk_exchange_rates_lifecycle",
        ),
        CheckConstraint(
            "authority_mode IN ('MANUAL', 'AUTOMATIC')",
            name="chk_exchange_rates_authority_mode",
        ),
        CheckConstraint(
            "validation_status IN ('ADMIN_APPROVED', 'VALIDATED')",
            name="chk_exchange_rates_validation_status",
        ),
        CheckConstraint(
            "(authority_mode = 'MANUAL' AND source = 'MBB_ADMIN' "
            "AND fetched_at IS NULL AND published_at IS NULL "
            "AND validated_at IS NULL AND validation_status = 'ADMIN_APPROVED') OR "
            "(authority_mode = 'AUTOMATIC' AND source = 'EXCHANGE_RATE_API' "
            "AND fetched_at IS NOT NULL AND published_at IS NOT NULL "
            "AND validated_at IS NOT NULL AND validation_status = 'VALIDATED')",
            name="chk_exchange_rates_provenance",
        ),
        CheckConstraint(
            "published_at IS NULL OR published_at <= fetched_at",
            name="chk_exchange_rates_provider_time",
        ),
        Index(
            "uq_exchange_rates_current_pair_mode",
            "base_currency",
            "quote_currency",
            "authority_mode",
            unique=True,
            postgresql_where=text("ended_at IS NULL"),
        ),
        Index(
            "idx_exchange_rates_history",
            "base_currency",
            "quote_currency",
            "effective_at",
        ),
        {"schema": "mbb"},
    )

    exchange_rate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    base_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    quote_currency: Mapped[str] = mapped_column(String(3), nullable=False)
    rate: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    authority_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="MANUAL"
    )
    source: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="MBB_ADMIN"
    )
    fetched_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    published_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    validated_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    validation_status: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="ADMIN_APPROVED"
    )
    effective_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )


class ExchangeRateAuthority(Base):
    __tablename__ = "exchange_rate_authorities"
    __table_args__ = (
        CheckConstraint(
            "base_currency = 'USD' AND quote_currency = 'CDF'",
            name="chk_exchange_rate_authorities_supported_pair",
        ),
        CheckConstraint(
            "mode IN ('MANUAL', 'AUTOMATIC')",
            name="chk_exchange_rate_authorities_mode",
        ),
        CheckConstraint("revision > 0", name="chk_exchange_rate_authorities_revision"),
        {"schema": "mbb"},
    )

    base_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    quote_currency: Mapped[str] = mapped_column(String(3), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_by_account_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
    )
