"""Authoritative USD price and Administrator-maintained FX persistence."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import TIMESTAMP, CheckConstraint, ForeignKey, Index, Numeric, String, text
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
        Index(
            "uq_exchange_rates_current_pair",
            "base_currency",
            "quote_currency",
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
    effective_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    ended_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
