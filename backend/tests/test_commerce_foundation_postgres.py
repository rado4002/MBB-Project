from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.models.catalog import Product, SellableItem
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import OperatorAuditEvent
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.modules.catalog.service import (
    CatalogConflict,
    create_product,
    create_sellable_item,
    update_product,
)
from app.modules.commerce_admin import (
    CommerceAdminContext,
    CommerceAuthorizationDenied,
)
from app.modules.inventory.service import get_inventory_status, set_inventory_status
from app.modules.pricing.service import (
    UnsupportedCurrency,
    get_current_cdf_quote,
    get_current_price,
    get_current_usd_cdf_rate,
    set_current_exchange_rate,
    set_current_usd_price,
)

RUNTIME_URL = os.environ.get("AI2B_TEST_DATABASE_URL")
PRICE_CONCURRENCY_URL = os.environ.get("AI2B_PRICE_CONCURRENCY_DATABASE_URL")
FX_CONCURRENCY_URL = os.environ.get("AI2B_FX_CONCURRENCY_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not RUNTIME_URL,
    reason="AI2B_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)

TRUNCATE = text(
    """
    TRUNCATE TABLE
        mbb.inventory_statuses,
        mbb.exchange_rates,
        mbb.sellable_item_prices,
        mbb.sellable_items,
        mbb.products,
        mbb.operator_audit_security_metadata,
        mbb.operator_audit_events,
        mbb.operator_accounts
    RESTART IDENTITY CASCADE
    """
)


def _account(role: str, suffix: str) -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized=f"commerce.{suffix}",
        display_name=f"Commerce {suffix.title()}",
        email_normalized=None,
        password_hash="not-used",
        role=role,
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


def _admin(account: OperatorAccount, request_id: str = "ai2b-postgres") -> CommerceAdminContext:
    return CommerceAdminContext(account.account_id, request_id)


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert RUNTIME_URL is not None
    database_engine = create_async_engine(RUNTIME_URL, pool_size=10)
    async with database_engine.begin() as connection:
        await connection.execute(TRUNCATE)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await database_engine.dispose()


async def _seed_accounts(engine: AsyncEngine) -> tuple[OperatorAccount, OperatorAccount]:
    admin = _account("administrator", "administrator")
    operator = _account("operator", "operator")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([admin, operator])
        await session.commit()
    return admin, operator


async def _seed_catalog(
    engine: AsyncEngine, admin: OperatorAccount
) -> tuple[Product, SellableItem]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        product = await create_product(
            session,
            name="Fictional Air Fryer",
            category_code="air_fryer",
            description="Fictional product used only for isolated tests.",
            active=True,
            administrator=_admin(admin),
        )
        item = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="Model 8L",
            sku="FICTIONAL-8L",
            attributes={"capacity_l": 8, "power_w": 1700},
            active=True,
            administrator=_admin(admin),
        )
        await session.commit()
        return product, item


@pytest.mark.asyncio
async def test_business_acceptance_scenario_proves_domain_ownership(engine: AsyncEngine) -> None:
    admin, _operator = await _seed_accounts(engine)
    product, item = await _seed_catalog(engine, admin)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        price_60 = await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=Decimal("60.00"),
            administrator=_admin(admin, "price-60"),
        )
        await set_inventory_status(
            session,
            sellable_item_id=item.sellable_item_id,
            status="available",
            administrator=_admin(admin, "inventory-available"),
        )
        rate_2800 = await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(admin, "rate-2800"),
        )
        await session.commit()

    async with factory() as session:
        persisted_product = await session.get(Product, product.product_id)
        persisted_item = await session.get(SellableItem, item.sellable_item_id)
        current_price = await get_current_price(session, item.sellable_item_id)
        inventory = await get_inventory_status(session, item.sellable_item_id)
        quote = await get_current_cdf_quote(session, item.sellable_item_id)
        assert persisted_product is not None and persisted_product.name == "Fictional Air Fryer"
        assert persisted_item is not None and persisted_item.model_label == "Model 8L"
        assert current_price is not None and current_price.amount == Decimal("60.00")
        assert inventory.configured is True and inventory.status == "available"
        assert quote is not None and quote.cdf_amount == Decimal("168000.00")

    async with factory() as session:
        price_65 = await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=Decimal("65.00"),
            administrator=_admin(admin, "price-65"),
        )
        await session.commit()

    async with factory() as session:
        prices = (
            await session.scalars(
                select(SellableItemPrice)
                .where(SellableItemPrice.sellable_item_id == item.sellable_item_id)
                .order_by(SellableItemPrice.effective_at)
            )
        ).all()
        assert [price.amount for price in prices] == [Decimal("60.00"), Decimal("65.00")]
        assert prices[0].price_id == price_60.price_id and prices[0].ended_at is not None
        assert prices[1].price_id == price_65.price_id and prices[1].ended_at is None
        assert (await session.get(Product, product.product_id)).name == "Fictional Air Fryer"
        assert (await get_inventory_status(session, item.sellable_item_id)).status == "available"

    async with factory() as session:
        await set_inventory_status(
            session,
            sellable_item_id=item.sellable_item_id,
            status="out_of_stock",
            administrator=_admin(admin, "inventory-out"),
        )
        await session.commit()
    async with factory() as session:
        assert (await get_inventory_status(session, item.sellable_item_id)).status == "out_of_stock"
        assert (await get_current_price(session, item.sellable_item_id)).amount == Decimal("65.00")

    async with factory() as session:
        rate_2900 = await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2900.000000"),
            administrator=_admin(admin, "rate-2900"),
        )
        await session.commit()
    async with factory() as session:
        quote = await get_current_cdf_quote(session, item.sellable_item_id)
        assert quote is not None and quote.usd_amount == Decimal("65.00")
        assert quote.cdf_amount == Decimal("188500.00")
        assert quote.exchange_rate_id == rate_2900.exchange_rate_id
        old_rate = await session.get(ExchangeRate, rate_2800.exchange_rate_id)
        assert old_rate is not None and old_rate.ended_at is not None
        assert (await session.get(Product, product.product_id)).name == "Fictional Air Fryer"
        assert (await get_inventory_status(session, item.sellable_item_id)).status == "out_of_stock"
        assert await session.scalar(select(func.count()).select_from(OperatorAuditEvent)) == 8


@pytest.mark.asyncio
async def test_constraints_missing_semantics_and_authorization_fail_closed(
    engine: AsyncEngine,
) -> None:
    admin, operator = await _seed_accounts(engine)
    product, item = await _seed_catalog(engine, admin)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        missing = await get_inventory_status(session, item.sellable_item_id)
        assert missing.configured is False and missing.status == "unknown"
        with pytest.raises(CommerceAuthorizationDenied):
            await create_product(
                session,
                name="Denied",
                category_code="denied",
                description="Must not be inserted.",
                active=True,
                administrator=_admin(operator, "operator-denied"),
            )
        assert await session.scalar(
            select(func.count()).select_from(Product).where(Product.name == "Denied")
        ) == 0
        with pytest.raises(UnsupportedCurrency):
            await set_current_usd_price(
                session,
                sellable_item_id=item.sellable_item_id,
                amount=Decimal("60.00"),
                currency="CDF",
                administrator=_admin(admin),
            )
        with pytest.raises(UnsupportedCurrency):
            await set_current_exchange_rate(
                session,
                base_currency="EUR",
                quote_currency="CDF",
                rate=Decimal("2800.000000"),
                administrator=_admin(admin),
            )
        await session.rollback()

    async with factory() as session:
        await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=Decimal("60.00"),
            administrator=_admin(admin, "missing-rate"),
        )
        await session.commit()
    async with factory() as session:
        quote_without_rate = await get_current_cdf_quote(session, item.sellable_item_id)
        assert quote_without_rate is not None
        assert quote_without_rate.usd_amount == Decimal("60.00")
        assert quote_without_rate.cdf_amount is None

    async def invalid(statement: str, parameters: dict | None = None) -> None:
        with pytest.raises(IntegrityError):
            async with engine.begin() as connection:
                await connection.execute(text(statement), parameters or {})

    await invalid(
        "INSERT INTO mbb.sellable_items (product_id, model_label) VALUES (:id, 'Bad FK')",
        {"id": uuid.uuid4()},
    )
    await invalid(
        "INSERT INTO mbb.sellable_items (product_id, sku) VALUES (:id, 'FICTIONAL-8L')",
        {"id": product.product_id},
    )
    await invalid(
        "INSERT INTO mbb.sellable_item_prices (sellable_item_id, amount, currency) "
        "VALUES (:id, 0, 'USD')",
        {"id": item.sellable_item_id},
    )
    await invalid(
        "INSERT INTO mbb.sellable_item_prices (sellable_item_id, amount, currency) "
        "VALUES (:id, -1, 'USD')",
        {"id": item.sellable_item_id},
    )
    await invalid(
        "INSERT INTO mbb.sellable_item_prices (sellable_item_id, amount, currency) "
        "VALUES (:id, 61, 'USD')",
        {"id": item.sellable_item_id},
    )
    await invalid(
        "INSERT INTO mbb.exchange_rates (base_currency, quote_currency, rate) "
        "VALUES ('USD', 'CDF', 0)"
    )
    await invalid(
        "INSERT INTO mbb.inventory_statuses (sellable_item_id, status) "
        "VALUES (:id, 'invalid')",
        {"id": item.sellable_item_id},
    )

    async with factory() as session:
        current = await get_current_price(session, item.sellable_item_id)
        assert current is not None
        current.ended_at = datetime.now(timezone.utc)
        await session.commit()
    async with factory() as session:
        assert await get_current_cdf_quote(session, item.sellable_item_id) is None
        await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=Decimal("60.00"),
            administrator=_admin(admin, "replacement-after-retirement"),
        )
        rate = await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(admin, "retired-rate"),
        )
        await session.commit()
    async with factory() as session:
        retired_rate = await session.get(ExchangeRate, rate.exchange_rate_id)
        assert retired_rate is not None
        retired_rate.ended_at = datetime.now(timezone.utc)
        await session.commit()
    async with factory() as session:
        quote_with_retired_rate = await get_current_cdf_quote(
            session, item.sellable_item_id
        )
        assert quote_with_retired_rate is not None
        assert quote_with_retired_rate.usd_amount == Decimal("60.00")
        assert quote_with_retired_rate.cdf_amount is None
        await set_inventory_status(
            session,
            sellable_item_id=item.sellable_item_id,
            status="unknown",
            administrator=_admin(admin, "configured-unknown"),
        )
        await session.commit()
    async with factory() as session:
        configured_unknown = await get_inventory_status(session, item.sellable_item_id)
        assert configured_unknown.configured is True
        assert configured_unknown.status == "unknown"
    await invalid(
        "INSERT INTO mbb.inventory_statuses (sellable_item_id, status) "
        "VALUES (:id, 'available')",
        {"id": item.sellable_item_id},
    )
    await invalid(
        "DELETE FROM mbb.products WHERE product_id = :id", {"id": product.product_id}
    )

    async with factory() as session:
        second = await create_product(
            session,
            name="Fictional Power Bank",
            category_code="power_bank",
            description="Second fictional product.",
            active=True,
            administrator=_admin(admin),
        )
        with pytest.raises(CatalogConflict):
            await create_sellable_item(
                session,
                product_id=second.product_id,
                model_label="Duplicate SKU",
                sku="fictional-8l",
                attributes={},
                active=True,
                administrator=_admin(admin),
            )
        await session.rollback()

    async with factory() as session:
        await update_product(
            session,
            product_id=product.product_id,
            active=False,
            administrator=_admin(admin),
        )
        await session.commit()
    async with factory() as session:
        assert (await session.get(Product, product.product_id)).active is False
        assert await session.get(SellableItem, item.sellable_item_id) is not None
        assert await session.scalar(
            select(func.count()).select_from(SellableItemPrice).where(
                SellableItemPrice.sellable_item_id == item.sellable_item_id
            )
        ) == 2
        assert (await get_inventory_status(session, item.sellable_item_id)).configured


async def _prepare_concurrency_database(
    database_url: str,
) -> tuple[AsyncEngine, OperatorAccount, SellableItem]:
    engine = create_async_engine(database_url, pool_size=10)
    async with engine.begin() as connection:
        await connection.execute(TRUNCATE)
    admin, _operator = await _seed_accounts(engine)
    _product, item = await _seed_catalog(engine, admin)
    return engine, admin, item


@pytest.mark.asyncio
@pytest.mark.skipif(
    not PRICE_CONCURRENCY_URL,
    reason="AI2B_PRICE_CONCURRENCY_DATABASE_URL is required",
)
async def test_concurrent_price_replacements_leave_one_current_usd_price() -> None:
    assert PRICE_CONCURRENCY_URL is not None
    engine, admin, item = await _prepare_concurrency_database(PRICE_CONCURRENCY_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def replace(amount: str) -> None:
        async with factory() as session:
            await set_current_usd_price(
                session,
                sellable_item_id=item.sellable_item_id,
                amount=Decimal(amount),
                administrator=_admin(admin, f"price-{amount}"),
            )
            await session.commit()

    try:
        await asyncio.gather(replace("60.00"), replace("65.00"))
        async with factory() as session:
            prices = (
                await session.scalars(
                    select(SellableItemPrice).where(
                        SellableItemPrice.sellable_item_id == item.sellable_item_id
                    )
                )
            ).all()
            assert len(prices) == 2
            assert sum(price.ended_at is None for price in prices) == 1
    finally:
        async with engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await engine.dispose()


@pytest.mark.asyncio
@pytest.mark.skipif(
    not FX_CONCURRENCY_URL,
    reason="AI2B_FX_CONCURRENCY_DATABASE_URL is required",
)
async def test_concurrent_rate_replacements_leave_one_current_usd_cdf_rate() -> None:
    assert FX_CONCURRENCY_URL is not None
    engine, admin, _item = await _prepare_concurrency_database(FX_CONCURRENCY_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def replace(rate: str) -> None:
        async with factory() as session:
            await set_current_exchange_rate(
                session,
                base_currency="USD",
                quote_currency="CDF",
                rate=Decimal(rate),
                administrator=_admin(admin, f"rate-{rate}"),
            )
            await session.commit()

    try:
        await asyncio.gather(replace("2800.000000"), replace("2900.000000"))
        async with factory() as session:
            rates = (await session.scalars(select(ExchangeRate))).all()
            assert len(rates) == 2
            assert sum(rate.ended_at is None for rate in rates) == 1
            assert (await get_current_usd_cdf_rate(session)) is not None
    finally:
        async with engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await engine.dispose()
