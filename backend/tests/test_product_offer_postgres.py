from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.models.catalog import Product, SellableItem
from app.models.operator_account import OperatorAccount
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.modules.catalog.service import create_product, create_sellable_item
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.inventory.service import set_inventory_status
from app.modules.pricing.service import set_current_exchange_rate, set_current_usd_price
from app.modules.product_offer.service import (
    ProductOfferCdfQuoteUnavailable,
    get_product_offer,
    search_product_offers,
)

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2B_TEST_DATABASE_URL is required for product offer PostgreSQL evidence",
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


def _account() -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="product.offer.admin",
        display_name="Product Offer Admin",
        email_normalized=None,
        password_hash="not-used",
        role="administrator",
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


def _admin(account: OperatorAccount) -> CommerceAdminContext:
    return CommerceAdminContext(account.account_id, "ai2c-product-offer")


async def _seed_admin(factory) -> OperatorAccount:
    admin = _account()
    async with factory() as session:
        session.add(admin)
        await session.commit()
    return admin


async def _create_product(
    factory,
    admin: OperatorAccount,
    *,
    name: str,
    active: bool = True,
) -> Product:
    async with factory() as session:
        product = await create_product(
            session,
            name=name,
            category_code="air_fryer",
            description="Fictional product used only for isolated tests.",
            active=active,
            administrator=_admin(admin),
        )
        await session.commit()
        return product


async def _create_item(
    factory,
    admin: OperatorAccount,
    product: Product,
    *,
    model_label: str,
    sku: str,
    active: bool = True,
) -> SellableItem:
    async with factory() as session:
        item = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label=model_label,
            sku=sku,
            attributes={"capacity_l": int(model_label.rstrip("L"))}
            if model_label.rstrip("L").isdigit()
            else {},
            active=active,
            administrator=_admin(admin),
        )
        await session.commit()
        return item


async def _set_price_inventory(
    factory,
    admin: OperatorAccount,
    item: SellableItem,
    *,
    amount: Decimal,
    status: str | None,
) -> None:
    async with factory() as session:
        await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=amount,
            administrator=_admin(admin),
        )
        if status is not None:
            await set_inventory_status(
                session,
                sellable_item_id=item.sellable_item_id,
                status=status,
                administrator=_admin(admin),
            )
        await session.commit()


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL)
    async with database_engine.begin() as connection:
        await connection.execute(TRUNCATE)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await database_engine.dispose()


@pytest.mark.asyncio
async def test_product_offer_exact_read_preserves_truthful_states(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_admin(factory)
    product = await _create_product(factory, admin, name="Fictional Air Fryer Pro")
    inactive_product = await _create_product(
        factory,
        admin,
        name="Fictional Retired Fryer",
        active=False,
    )

    sellable = await _create_item(
        factory,
        admin,
        product,
        model_label="6L",
        sku="AIR-FRYER-6L",
    )
    out_of_stock = await _create_item(
        factory,
        admin,
        product,
        model_label="8L",
        sku="AIR-FRYER-8L",
    )
    configured_unknown = await _create_item(
        factory,
        admin,
        product,
        model_label="10L",
        sku="AIR-FRYER-10L",
    )
    missing_inventory = await _create_item(
        factory,
        admin,
        product,
        model_label="12L",
        sku="AIR-FRYER-12L",
    )
    no_current_price = await _create_item(
        factory,
        admin,
        product,
        model_label="14L",
        sku="AIR-FRYER-14L",
    )
    inactive_item = await _create_item(
        factory,
        admin,
        product,
        model_label="16L",
        sku="AIR-FRYER-16L",
        active=False,
    )
    item_under_inactive_product = await _create_item(
        factory,
        admin,
        inactive_product,
        model_label="18L",
        sku="AIR-FRYER-18L",
    )

    await _set_price_inventory(
        factory,
        admin,
        sellable,
        amount=Decimal("55.00"),
        status="available",
    )
    await _set_price_inventory(
        factory,
        admin,
        out_of_stock,
        amount=Decimal("70.00"),
        status="out_of_stock",
    )
    await _set_price_inventory(
        factory,
        admin,
        configured_unknown,
        amount=Decimal("75.00"),
        status="unknown",
    )
    await _set_price_inventory(
        factory,
        admin,
        missing_inventory,
        amount=Decimal("80.00"),
        status=None,
    )
    await _set_price_inventory(
        factory,
        admin,
        no_current_price,
        amount=Decimal("85.00"),
        status="available",
    )
    await _set_price_inventory(
        factory,
        admin,
        inactive_item,
        amount=Decimal("90.00"),
        status="available",
    )
    await _set_price_inventory(
        factory,
        admin,
        item_under_inactive_product,
        amount=Decimal("95.00"),
        status="available",
    )
    async with factory() as session:
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(admin),
        )
        current = await session.get(SellableItemPrice, no_current_price.sellable_item_id)
        assert current is None
        price = (
            await session.execute(
                text(
                    "SELECT price_id FROM mbb.sellable_item_prices "
                    "WHERE sellable_item_id = :id AND ended_at IS NULL"
                ),
                {"id": no_current_price.sellable_item_id},
            )
        ).scalar_one()
        row = await session.get(SellableItemPrice, price)
        assert row is not None
        row.ended_at = datetime.now(timezone.utc)
        await session.commit()

    async with factory() as session:
        sellable_offer = await get_product_offer(session, sellable.sellable_item_id)
        out_offer = await get_product_offer(session, out_of_stock.sellable_item_id)
        unknown_offer = await get_product_offer(
            session, configured_unknown.sellable_item_id
        )
        missing_offer = await get_product_offer(
            session, missing_inventory.sellable_item_id
        )
        no_price_offer = await get_product_offer(
            session, no_current_price.sellable_item_id
        )
        inactive_item_offer = await get_product_offer(
            session, inactive_item.sellable_item_id
        )
        inactive_product_offer = await get_product_offer(
            session, item_under_inactive_product.sellable_item_id
        )

    assert sellable_offer is not None
    assert sellable_offer.product_id == product.product_id
    assert sellable_offer.sellable_item_id == sellable.sellable_item_id
    assert sellable_offer.offer_status == "sellable_now"
    assert sellable_offer.current_usd_price == Decimal("55.00")
    assert sellable_offer.derived_cdf_quote is not None
    assert sellable_offer.derived_cdf_quote.cdf_amount == Decimal("154000.00")
    assert out_offer is not None and out_offer.offer_status == "out_of_stock"
    assert (
        unknown_offer is not None
        and unknown_offer.offer_status == "availability_unconfirmed"
        and unknown_offer.inventory_configured is True
    )
    assert (
        missing_offer is not None
        and missing_offer.offer_status == "availability_unconfirmed"
        and missing_offer.inventory_configured is False
    )
    assert (
        no_price_offer is not None
        and no_price_offer.offer_status == "price_unavailable"
        and no_price_offer.current_usd_price is None
    )
    assert inactive_item_offer is not None and inactive_item_offer.offer_status == "inactive"
    assert (
        inactive_product_offer is not None
        and inactive_product_offer.offer_status == "inactive"
    )


@pytest.mark.asyncio
async def test_product_offer_current_price_and_fx_history(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_admin(factory)
    product = await _create_product(factory, admin, name="Fictional History Fryer")
    item = await _create_item(
        factory,
        admin,
        product,
        model_label="6L",
        sku="HISTORY-FRYER-6L",
    )
    await _set_price_inventory(
        factory,
        admin,
        item,
        amount=Decimal("40.00"),
        status="available",
    )
    async with factory() as session:
        await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=Decimal("45.00"),
            administrator=_admin(admin),
        )
        await set_current_usd_price(
            session,
            sellable_item_id=item.sellable_item_id,
            amount=Decimal("50.00"),
            administrator=_admin(admin),
        )
        first_rate = await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(admin),
        )
        await session.commit()

    async with factory() as session:
        offer = await get_product_offer(session, item.sellable_item_id)
        assert offer is not None
        assert offer.current_usd_price == Decimal("50.00")
        assert offer.derived_cdf_quote is not None
        assert offer.derived_cdf_quote.cdf_amount == Decimal("140000.00")
        first_seen_price_id = offer.price_id
        search_results = await search_product_offers(session, query="history fryer")
        assert [result.sellable_item_id for result in search_results].count(
            item.sellable_item_id
        ) == 1

    async with factory() as session:
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("3000.000000"),
            administrator=_admin(admin),
        )
        await session.commit()

    async with factory() as session:
        offer = await get_product_offer(session, item.sellable_item_id)
        assert offer is not None
        assert offer.current_usd_price == Decimal("50.00")
        assert offer.price_id == first_seen_price_id
        assert offer.derived_cdf_quote is not None
        assert offer.derived_cdf_quote.cdf_amount == Decimal("150000.00")
        old_rate = await session.get(ExchangeRate, first_rate.exchange_rate_id)
        assert old_rate is not None and old_rate.ended_at is not None

        current_rate = (
            await session.execute(
                text(
                    "SELECT exchange_rate_id FROM mbb.exchange_rates "
                    "WHERE ended_at IS NULL"
                )
            )
        ).scalar_one()
        row = await session.get(ExchangeRate, current_rate)
        assert row is not None
        row.ended_at = datetime.now(timezone.utc)
        await session.commit()

    async with factory() as session:
        offer = await get_product_offer(session, item.sellable_item_id)
        assert offer is not None
        assert offer.current_usd_price == Decimal("50.00")
        assert offer.cdf_quote_status == "cdf_quote_unavailable"
        assert offer.cdf_quote_unavailable_reason == "current_fx_unavailable"
        assert offer.derived_cdf_quote is None


@pytest.mark.asyncio
async def test_product_offer_search_modes_budgets_and_determinism(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_admin(factory)
    product = await _create_product(factory, admin, name="Fictional Search Fryer")
    inactive_product = await _create_product(
        factory,
        admin,
        name="Fictional Inactive Search Fryer",
        active=False,
    )
    sellable = await _create_item(
        factory, admin, product, model_label="6L", sku="SEARCH-FRYER-6L"
    )
    out_of_stock = await _create_item(
        factory, admin, product, model_label="8L", sku="SEARCH-FRYER-8L"
    )
    missing_inventory = await _create_item(
        factory, admin, product, model_label="10L", sku="SEARCH-FRYER-10L"
    )
    no_price = await _create_item(
        factory, admin, product, model_label="12L", sku="SEARCH-FRYER-12L"
    )
    inactive = await _create_item(
        factory, admin, inactive_product, model_label="14L", sku="SEARCH-FRYER-14L"
    )
    await _set_price_inventory(
        factory, admin, sellable, amount=Decimal("55.00"), status="available"
    )
    await _set_price_inventory(
        factory, admin, out_of_stock, amount=Decimal("70.00"), status="out_of_stock"
    )
    await _set_price_inventory(
        factory, admin, missing_inventory, amount=Decimal("75.00"), status=None
    )
    await _set_price_inventory(
        factory, admin, inactive, amount=Decimal("40.00"), status="available"
    )
    async with factory() as session:
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(admin),
        )
        await session.commit()

    async with factory() as session:
        sellable_only = await search_product_offers(
            session, query="search fryer", search_mode="sellable_only"
        )
        include_unavailable = await search_product_offers(
            session, query="search fryer", search_mode="include_unavailable"
        )
        usd_budget_results = await search_product_offers(
            session,
            query="search fryer",
            max_budget_usd=Decimal("60.00"),
            search_mode="include_unavailable",
        )
        cdf_budget_results = await search_product_offers(
            session,
            query="search fryer",
            max_budget_cdf=Decimal("160000.00"),
            search_mode="include_unavailable",
        )
        limited = await search_product_offers(
            session,
            query="search fryer",
            search_mode="include_unavailable",
            limit=2,
        )

    assert [offer.sellable_item_id for offer in sellable_only] == [
        sellable.sellable_item_id
    ]
    include_ids = [offer.sellable_item_id for offer in include_unavailable]
    assert include_ids == [
        sellable.sellable_item_id,
        missing_inventory.sellable_item_id,
        out_of_stock.sellable_item_id,
        no_price.sellable_item_id,
    ]
    assert inactive.sellable_item_id not in include_ids
    assert [offer.sellable_item_id for offer in usd_budget_results] == [
        sellable.sellable_item_id
    ]
    assert [offer.sellable_item_id for offer in cdf_budget_results] == [
        sellable.sellable_item_id
    ]
    assert [offer.sellable_item_id for offer in limited] == [
        sellable.sellable_item_id,
        missing_inventory.sellable_item_id,
    ]


@pytest.mark.asyncio
async def test_cdf_budget_without_current_fx_fails_with_typed_error(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin = await _seed_admin(factory)
    product = await _create_product(factory, admin, name="Fictional Missing FX Fryer")
    item = await _create_item(
        factory, admin, product, model_label="6L", sku="MISSING-FX-FRYER-6L"
    )
    await _set_price_inventory(
        factory, admin, item, amount=Decimal("55.00"), status="available"
    )

    async with factory() as session:
        with pytest.raises(ProductOfferCdfQuoteUnavailable) as exc_info:
            await search_product_offers(
                session,
                query="missing fx fryer",
                max_budget_cdf=Decimal("160000.00"),
            )

    assert exc_info.value.code == "CDF_QUOTE_UNAVAILABLE"
