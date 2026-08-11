"""Pricing-owned current USD prices, FX policy, and derived CDF quotes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.catalog import SellableItem
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.modules.commerce_admin import (
    CommerceAdminContext,
    require_commerce_administrator,
)
from app.operator_identity.audit import append_operator_audit_event

USD = "USD"
CDF = "CDF"
CDF_QUOTE_QUANTUM = Decimal("0.01")
USD_AMOUNT_MAX = Decimal("9999999999.99")
EXCHANGE_RATE_MAX = Decimal("999999999999.999999")
EXCHANGE_RATE_QUANTUM = Decimal("0.000001")


class PricingNotFound(Exception):
    pass


class UnsupportedCurrency(Exception):
    pass


@dataclass(frozen=True)
class DerivedCdfQuote:
    price_id: uuid.UUID
    usd_amount: Decimal
    exchange_rate_id: uuid.UUID | None
    usd_to_cdf_rate: Decimal | None
    cdf_amount: Decimal | None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def calculate_cdf_amount(usd_amount: Decimal, rate: Decimal) -> Decimal:
    """Round the derived backend monetary quote to cents, half away from zero."""
    return (usd_amount * rate).quantize(CDF_QUOTE_QUANTUM, rounding=ROUND_HALF_UP)


def _validate_usd_amount(amount: Decimal) -> None:
    if (
        not isinstance(amount, Decimal)
        or not amount.is_finite()
        or amount <= 0
        or amount > USD_AMOUNT_MAX
        or amount != amount.quantize(CDF_QUOTE_QUANTUM)
    ):
        raise ValueError("USD amount must be a positive Numeric(12,2) Decimal")


def _validate_exchange_rate(rate: Decimal) -> None:
    if (
        not isinstance(rate, Decimal)
        or not rate.is_finite()
        or rate <= 0
        or rate > EXCHANGE_RATE_MAX
        or rate != rate.quantize(EXCHANGE_RATE_QUANTUM)
    ):
        raise ValueError("exchange rate must be a positive Numeric(18,6) Decimal")


async def get_current_price(
    session: AsyncSession, sellable_item_id: uuid.UUID, currency: str = USD
) -> SellableItemPrice | None:
    return await session.scalar(
        select(SellableItemPrice).where(
            SellableItemPrice.sellable_item_id == sellable_item_id,
            SellableItemPrice.currency == currency,
            SellableItemPrice.ended_at.is_(None),
        )
    )


async def list_price_history(
    session: AsyncSession,
    sellable_item_id: uuid.UUID,
    *,
    currency: str = USD,
    limit: int = 100,
) -> list[SellableItemPrice]:
    return list(
        (
            await session.scalars(
                select(SellableItemPrice)
                .where(
                    SellableItemPrice.sellable_item_id == sellable_item_id,
                    SellableItemPrice.currency == currency,
                )
                .order_by(
                    SellableItemPrice.effective_at.desc(),
                    SellableItemPrice.price_id.desc(),
                )
                .limit(limit)
            )
        ).all()
    )


async def set_current_usd_price(
    session: AsyncSession,
    *,
    sellable_item_id: uuid.UUID,
    amount: Decimal,
    administrator: CommerceAdminContext,
    currency: str = USD,
    now: datetime | None = None,
) -> SellableItemPrice:
    if currency != USD:
        raise UnsupportedCurrency("USD is the only authoritative base price currency")
    _validate_usd_amount(amount)
    actor = await require_commerce_administrator(session, administrator)
    item = await session.scalar(
        select(SellableItem)
        .where(SellableItem.sellable_item_id == sellable_item_id)
        .with_for_update()
    )
    if item is None:
        raise PricingNotFound("sellable item was not found")
    event_time = now or _utcnow()
    current = await session.scalar(
        select(SellableItemPrice)
        .where(
            SellableItemPrice.sellable_item_id == sellable_item_id,
            SellableItemPrice.currency == USD,
            SellableItemPrice.ended_at.is_(None),
        )
        .with_for_update()
    )
    previous_price_id: uuid.UUID | None = None
    if current is not None:
        previous_price_id = current.price_id
        current.ended_at = event_time
        await session.flush()
    replacement = SellableItemPrice(
        sellable_item_id=sellable_item_id,
        amount=amount,
        currency=USD,
        effective_at=event_time,
        ended_at=None,
    )
    session.add(replacement)
    await session.flush()
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.usd_price.replaced",
        target_type="sellable_item_price",
        target_id=str(replacement.price_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={
            "sellable_item_id": str(sellable_item_id),
            "currency": USD,
            "previous_price_id": str(previous_price_id) if previous_price_id else None,
        },
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return replacement


async def get_current_usd_cdf_rate(session: AsyncSession) -> ExchangeRate | None:
    return await session.scalar(
        select(ExchangeRate).where(
            ExchangeRate.base_currency == USD,
            ExchangeRate.quote_currency == CDF,
            ExchangeRate.ended_at.is_(None),
        )
    )


async def list_usd_cdf_rate_history(
    session: AsyncSession, *, limit: int = 100
) -> list[ExchangeRate]:
    return list(
        (
            await session.scalars(
                select(ExchangeRate)
                .where(
                    ExchangeRate.base_currency == USD,
                    ExchangeRate.quote_currency == CDF,
                )
                .order_by(ExchangeRate.effective_at.desc(), ExchangeRate.exchange_rate_id.desc())
                .limit(limit)
            )
        ).all()
    )


async def set_current_exchange_rate(
    session: AsyncSession,
    *,
    base_currency: str,
    quote_currency: str,
    rate: Decimal,
    administrator: CommerceAdminContext,
    now: datetime | None = None,
) -> ExchangeRate:
    if (base_currency, quote_currency) != (USD, CDF):
        raise UnsupportedCurrency("USD to CDF is the only supported exchange-rate pair")
    _validate_exchange_rate(rate)
    actor = await require_commerce_administrator(session, administrator)
    # There is no stable pair row before the first rate. This transaction-level
    # PostgreSQL advisory lock serializes the one supported policy scope.
    await session.execute(text("SELECT pg_advisory_xact_lock(723201)"))
    event_time = now or _utcnow()
    current = await session.scalar(
        select(ExchangeRate)
        .where(
            ExchangeRate.base_currency == USD,
            ExchangeRate.quote_currency == CDF,
            ExchangeRate.ended_at.is_(None),
        )
        .with_for_update()
    )
    previous_rate_id: uuid.UUID | None = None
    if current is not None:
        previous_rate_id = current.exchange_rate_id
        current.ended_at = event_time
        await session.flush()
    replacement = ExchangeRate(
        base_currency=USD,
        quote_currency=CDF,
        rate=rate,
        effective_at=event_time,
        ended_at=None,
    )
    session.add(replacement)
    await session.flush()
    await append_operator_audit_event(
        session,
        category="business",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=administrator.request_id,
        action="commerce.usd_cdf_rate.replaced",
        target_type="exchange_rate",
        target_id=str(replacement.exchange_rate_id),
        reason_code="commerce_administrator",
        outcome="succeeded",
        metadata={
            "base_currency": USD,
            "quote_currency": CDF,
            "previous_exchange_rate_id": (
                str(previous_rate_id) if previous_rate_id else None
            ),
        },
        source_network_fingerprint=administrator.source_network_fingerprint,
        user_agent_fingerprint=administrator.user_agent_fingerprint,
        occurred_at=event_time,
    )
    return replacement


async def get_current_cdf_quote(
    session: AsyncSession, sellable_item_id: uuid.UUID
) -> DerivedCdfQuote | None:
    price = await get_current_price(session, sellable_item_id, USD)
    if price is None:
        return None
    rate = await get_current_usd_cdf_rate(session)
    if rate is None:
        return DerivedCdfQuote(
            price_id=price.price_id,
            usd_amount=price.amount,
            exchange_rate_id=None,
            usd_to_cdf_rate=None,
            cdf_amount=None,
        )
    return DerivedCdfQuote(
        price_id=price.price_id,
        usd_amount=price.amount,
        exchange_rate_id=rate.exchange_rate_id,
        usd_to_cdf_rate=rate.rate,
        cdf_amount=calculate_cdf_amount(price.amount, rate.rate),
    )
