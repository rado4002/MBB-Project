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
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.models.catalog import Product, ProductMedia, SellableItem
from app.models.operator_account import OperatorAccount
from app.modules.catalog.service import (
    CatalogConflict,
    CatalogNotFound,
    create_product_media,
    get_effective_primary_media,
    list_product_media,
    list_sellable_item_media,
    set_primary_media,
    update_product_media,
)
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.inventory.service import set_inventory_status
from app.modules.pricing.service import (
    set_current_exchange_rate,
    set_current_usd_price,
)
from app.modules.product_offer.service import get_product_offer, search_product_offers

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2B_TEST_DATABASE_URL is required for Product Media PostgreSQL evidence",
)

TRUNCATE = text(
    """
    TRUNCATE TABLE
        mbb.product_media,
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
        username_normalized="media.admin",
        display_name="Media Administrator",
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


def _admin(account: OperatorAccount, request_id: str = "ai2c1-media") -> CommerceAdminContext:
    return CommerceAdminContext(
        actor_account_id=account.account_id,
        request_id=request_id,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL, pool_size=10)
    async with database_engine.begin() as connection:
        await connection.execute(TRUNCATE)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await database_engine.dispose()


async def _seed_catalog(
    factory: async_sessionmaker,
) -> tuple[OperatorAccount, Product, SellableItem, SellableItem]:
    account = _account()
    product = Product(
        name="Fictional Air Fryer",
        category_code="air_fryer",
        description="Fictional Product Media PostgreSQL fixture.",
        active=True,
    )
    item_6l = SellableItem(
        product_id=product.product_id,
        model_label="6L",
        sku="MEDIA-FRYER-6L",
        attributes={"capacity_l": 6},
        active=True,
    )
    item_8l = SellableItem(
        product_id=product.product_id,
        model_label="8L",
        sku="MEDIA-FRYER-8L",
        attributes={"capacity_l": 8},
        active=True,
    )
    async with factory() as session:
        session.add_all([account, product])
        await session.flush()
        item_6l.product_id = product.product_id
        item_8l.product_id = product.product_id
        session.add_all([item_6l, item_8l])
        await session.commit()
    return account, product, item_6l, item_8l


async def _create_media(
    factory: async_sessionmaker,
    account: OperatorAccount,
    *,
    product_id: uuid.UUID | None = None,
    sellable_item_id: uuid.UUID | None = None,
    suffix: str,
    is_primary: bool = False,
    active: bool = True,
) -> ProductMedia:
    async with factory() as session:
        media = await create_product_media(
            session,
            product_id=product_id,
            sellable_item_id=sellable_item_id,
            asset_url=f"https://example.invalid/product/{suffix}.jpg",
            alt_text=f"Fictional {suffix}",
            is_primary=is_primary,
            display_order=0,
            active=active,
            administrator=_admin(account),
        )
        await session.commit()
        return media


@pytest.mark.asyncio
async def test_media_ownership_reads_bounds_and_lifecycle(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account, product, item_6l, _item_8l = await _seed_catalog(factory)
    product_media = await _create_media(
        factory, account, product_id=product.product_id, suffix="product"
    )
    item_media = await _create_media(
        factory,
        account,
        sellable_item_id=item_6l.sellable_item_id,
        suffix="model6l",
    )

    async with factory() as session:
        assert [m.media_id for m in await list_product_media(session, product.product_id)] == [
            product_media.media_id
        ]
        assert [
            m.media_id
            for m in await list_sellable_item_media(session, item_6l.sellable_item_id)
        ] == [item_media.media_id]
        with pytest.raises(CatalogNotFound):
            await create_product_media(
                session,
                product_id=uuid.uuid4(),
                sellable_item_id=None,
                asset_url="https://example.invalid/product/missing.jpg",
                alt_text=None,
                is_primary=False,
                display_order=0,
                active=True,
                administrator=_admin(account),
            )
        await session.rollback()
        with pytest.raises(CatalogNotFound):
            await create_product_media(
                session,
                product_id=None,
                sellable_item_id=uuid.uuid4(),
                asset_url="https://example.invalid/product/missing-item.jpg",
                alt_text=None,
                is_primary=False,
                display_order=0,
                active=True,
                administrator=_admin(account),
            )
        await session.rollback()

    async with factory() as session:
        await update_product_media(
            session,
            media_id=item_media.media_id,
            active=False,
            administrator=_admin(account),
        )
        await session.commit()
    async with factory() as session:
        assert await list_sellable_item_media(session, item_6l.sellable_item_id) == []
        stored = await session.get(ProductMedia, item_media.media_id)
        assert stored is not None and stored.active is False
        await update_product_media(
            session,
            media_id=item_media.media_id,
            active=True,
            administrator=_admin(account),
        )
        await session.commit()

    for index in range(9):
        await _create_media(
            factory,
            account,
            product_id=product.product_id,
            suffix=f"gallery-{index}",
        )
    async with factory() as session:
        with pytest.raises(CatalogConflict):
            await create_product_media(
                session,
                product_id=product.product_id,
                sellable_item_id=None,
                asset_url="https://example.invalid/product/overflow.jpg",
                alt_text=None,
                is_primary=False,
                display_order=10,
                active=True,
                administrator=_admin(account),
            )


@pytest.mark.asyncio
async def test_database_enforces_owner_and_primary_invariants(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    _account_row, product, item_6l, _item_8l = await _seed_catalog(factory)
    async with factory() as session:
        for invalid in (
            ProductMedia(
                product_id=None,
                sellable_item_id=None,
                asset_url="https://example.invalid/product/neither.jpg",
                display_order=0,
            ),
            ProductMedia(
                product_id=product.product_id,
                sellable_item_id=item_6l.sellable_item_id,
                asset_url="https://example.invalid/product/both.jpg",
                display_order=0,
            ),
        ):
            with pytest.raises(IntegrityError):
                async with session.begin_nested():
                    session.add(invalid)
                    await session.flush()

        session.add_all(
            [
                ProductMedia(
                    product_id=product.product_id,
                    asset_url="https://example.invalid/product/primary-a.jpg",
                    is_primary=True,
                    active=True,
                    display_order=0,
                ),
                ProductMedia(
                    product_id=product.product_id,
                    asset_url="https://example.invalid/product/primary-b.jpg",
                    is_primary=True,
                    active=True,
                    display_order=1,
                ),
            ]
        )
        with pytest.raises(IntegrityError):
            await session.flush()


@pytest.mark.asyncio
async def test_primary_replacement_fallback_and_offer_projection(engine: AsyncEngine) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account, product, item_6l, item_8l = await _seed_catalog(factory)
    async with factory() as session:
        for item, amount in ((item_6l, Decimal("55.00")), (item_8l, Decimal("65.00"))):
            await set_current_usd_price(
                session,
                sellable_item_id=item.sellable_item_id,
                amount=amount,
                administrator=_admin(account),
            )
            await set_inventory_status(
                session,
                sellable_item_id=item.sellable_item_id,
                status="available",
                administrator=_admin(account),
            )
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(account),
        )
        await session.commit()
    product_media = await _create_media(
        factory,
        account,
        product_id=product.product_id,
        suffix="product",
        is_primary=True,
    )
    replacement = await _create_media(
        factory, account, product_id=product.product_id, suffix="product-replacement"
    )
    async with factory() as session:
        await set_primary_media(
            session,
            media_id=replacement.media_id,
            administrator=_admin(account),
        )
        await session.commit()
        old_primary = await session.get(ProductMedia, product_media.media_id)
        assert old_primary is not None and old_primary.is_primary is False
    product_media = replacement
    item_media = await _create_media(
        factory,
        account,
        sellable_item_id=item_8l.sellable_item_id,
        suffix="model8l",
        is_primary=True,
    )
    item_replacement = await _create_media(
        factory,
        account,
        sellable_item_id=item_8l.sellable_item_id,
        suffix="model8l-replacement",
    )
    async with factory() as session:
        await set_primary_media(
            session,
            media_id=item_replacement.media_id,
            administrator=_admin(account),
        )
        await session.commit()
        previous_item_primary = await session.get(ProductMedia, item_media.media_id)
        current_product_primary = await session.get(ProductMedia, product_media.media_id)
        assert previous_item_primary is not None
        assert previous_item_primary.is_primary is False
        assert current_product_primary is not None
        assert current_product_primary.is_primary is True
    item_media = item_replacement

    async with factory() as session:
        offer_6l = await get_product_offer(session, item_6l.sellable_item_id)
        offer_8l = await get_product_offer(session, item_8l.sellable_item_id)
        results = await search_product_offers(session, query="Fictional Air Fryer")
    assert offer_6l is not None and offer_6l.primary_media is not None
    assert offer_6l.primary_media.media_id == product_media.media_id
    assert offer_6l.primary_media.source_scope == "product"
    assert offer_8l is not None and offer_8l.primary_media is not None
    assert offer_8l.primary_media.media_id == item_media.media_id
    assert offer_8l.primary_media.source_scope == "sellable_item"
    assert offer_6l.offer_status == offer_8l.offer_status == "sellable_now"
    assert len(results) == 2
    assert len({offer.sellable_item_id for offer in results}) == 2

    async with factory() as session:
        effective = await get_effective_primary_media(session, item_8l.sellable_item_id)
        assert effective is not None and effective.media_id == item_media.media_id
        await update_product_media(
            session,
            media_id=item_media.media_id,
            active=False,
            administrator=_admin(account),
        )
        await session.commit()
    async with factory() as session:
        fallback = await get_product_offer(session, item_8l.sellable_item_id)
        assert fallback is not None and fallback.primary_media is not None
        assert fallback.primary_media.media_id == product_media.media_id
        assert fallback.primary_media.source_scope == "product"
        assert fallback.offer_status == "sellable_now"
        await update_product_media(
            session,
            media_id=product_media.media_id,
            active=False,
            administrator=_admin(account),
        )
        await session.commit()
    async with factory() as session:
        offer_6l = await get_product_offer(session, item_6l.sellable_item_id)
        offer_8l = await get_product_offer(session, item_8l.sellable_item_id)
        assert offer_6l is not None and offer_6l.primary_media is None
        assert offer_8l is not None and offer_8l.primary_media is None
        assert offer_6l.current_usd_price == Decimal("55.00")
        assert offer_8l.current_usd_price == Decimal("65.00")
        assert offer_6l.inventory_status == offer_8l.inventory_status == "available"
        assert offer_6l.offer_status == offer_8l.offer_status == "sellable_now"
        assert offer_6l.derived_cdf_quote is not None
        assert offer_8l.derived_cdf_quote is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_scope", ["product", "sellable_item"])
async def test_concurrent_primary_assignment_leaves_one_primary(
    engine: AsyncEngine, owner_scope: str
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account, product, item_6l, _item_8l = await _seed_catalog(factory)
    owner = (
        {"product_id": product.product_id}
        if owner_scope == "product"
        else {"sellable_item_id": item_6l.sellable_item_id}
    )
    first = await _create_media(factory, account, suffix=f"{owner_scope}-a", **owner)
    second = await _create_media(factory, account, suffix=f"{owner_scope}-b", **owner)

    async def assign(media_id: uuid.UUID) -> str:
        async with factory() as session:
            try:
                await set_primary_media(
                    session,
                    media_id=media_id,
                    administrator=_admin(account, request_id=f"primary-{media_id}"),
                )
                await session.commit()
                return "committed"
            except CatalogConflict:
                await session.rollback()
                return "conflict"

    outcomes = await asyncio.gather(assign(first.media_id), assign(second.media_id))
    assert all(outcome in {"committed", "conflict"} for outcome in outcomes)
    async with factory() as session:
        owner_filter = (
            ProductMedia.product_id == product.product_id
            if owner_scope == "product"
            else ProductMedia.sellable_item_id == item_6l.sellable_item_id
        )
        count = await session.scalar(
            select(func.count()).select_from(ProductMedia).where(
                owner_filter,
                ProductMedia.active.is_(True),
                ProductMedia.is_primary.is_(True),
            )
        )
        assert count == 1
