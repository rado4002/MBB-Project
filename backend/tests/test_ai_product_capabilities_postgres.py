from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app import database
from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityErrorCategory,
    CapabilityExecutor,
    CapabilityFailure,
    CapabilitySuccess,
    TrustedCapabilityContext,
)
from app.models.operator_account import OperatorAccount
from app.modules.catalog.service import (
    create_product,
    create_product_media,
    create_sellable_item,
)
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.inventory.service import set_inventory_status
from app.modules.pricing.service import (
    set_current_exchange_rate,
    set_current_usd_price,
)

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2B_TEST_DATABASE_URL is required for AI product capability evidence",
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

BUSINESS_TABLES = (
    "products",
    "sellable_items",
    "sellable_item_prices",
    "exchange_rates",
    "inventory_statuses",
    "product_media",
    "customers",
    "leads",
    "orders",
    "payments",
    "conversations",
    "operator_audit_events",
)


def _context() -> TrustedCapabilityContext:
    return TrustedCapabilityContext(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        expected_ownership_version=1,
    )


def _account() -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="ai2d.admin",
        display_name="AI-2D Administrator",
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
    return CommerceAdminContext(
        actor_account_id=account.account_id,
        request_id="ai2d-postgres-fixture",
    )


async def _snapshot(factory: async_sessionmaker) -> dict[str, str]:
    snapshots = {}
    async with factory() as session:
        for table_name in BUSINESS_TABLES:
            snapshots[table_name] = await session.scalar(
                text(
                    "SELECT COALESCE(jsonb_agg(to_jsonb(row_data) "
                    "ORDER BY to_jsonb(row_data)::text), '[]'::jsonb)::text "
                    f"FROM mbb.{table_name} AS row_data"
                )
            )
    return snapshots


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
async def test_executor_reads_authoritative_product_offers_without_business_writes(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    account = _account()
    async with factory() as session:
        session.add(account)
        await session.commit()

    async with factory() as session:
        product = await create_product(
            session,
            name="Fictional Air Fryer",
            category_code="air_fryer",
            description="Fictional AI-2D PostgreSQL capability fixture.",
            active=True,
            administrator=_admin(account),
        )
        item_6l = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="6L",
            sku="AI2D-FRYER-6L",
            attributes={"capacity_l": 6},
            active=True,
            administrator=_admin(account),
        )
        item_8l = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="8L",
            sku="AI2D-FRYER-8L",
            attributes={"capacity_l": 8},
            active=True,
            administrator=_admin(account),
        )
        await set_current_usd_price(
            session,
            sellable_item_id=item_6l.sellable_item_id,
            amount=Decimal("55.00"),
            administrator=_admin(account),
        )
        await set_current_usd_price(
            session,
            sellable_item_id=item_8l.sellable_item_id,
            amount=Decimal("70.00"),
            administrator=_admin(account),
        )
        await set_inventory_status(
            session,
            sellable_item_id=item_6l.sellable_item_id,
            status="available",
            administrator=_admin(account),
        )
        await set_inventory_status(
            session,
            sellable_item_id=item_8l.sellable_item_id,
            status="out_of_stock",
            administrator=_admin(account),
        )
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(account),
        )
        product_media = await create_product_media(
            session,
            product_id=product.product_id,
            sellable_item_id=None,
            asset_url="https://example.invalid/products/fryer.jpg",
            alt_text="Fictional air fryer product image",
            is_primary=True,
            display_order=0,
            active=True,
            administrator=_admin(account),
        )
        item_media = await create_product_media(
            session,
            product_id=None,
            sellable_item_id=item_8l.sellable_item_id,
            asset_url="https://example.invalid/products/fryer-8l.jpg",
            alt_text="Fictional 8L air fryer model image",
            is_primary=True,
            display_order=0,
            active=True,
            administrator=_admin(account),
        )
        await session.commit()

    monkeypatch.setattr(database, "async_session_factory", factory)
    executor = CapabilityExecutor(AI_CAPABILITY_REGISTRY)
    context = _context()
    before_reads = await _snapshot(factory)

    sellable_only = await executor.execute(
        requested_name="search_products",
        model_arguments={"query": "Air Fryer"},
        allowed_capabilities={"search_products"},
        context=context,
    )
    include_unavailable = await executor.execute(
        requested_name="search_products",
        model_arguments={
            "category_code": "air_fryer",
            "search_mode": "INCLUDE_UNAVAILABLE",
        },
        allowed_capabilities={"search_products"},
        context=context,
    )
    usd_budget = await executor.execute(
        requested_name="search_products",
        model_arguments={
            "max_budget": "60.00",
            "budget_currency": "USD",
            "search_mode": "INCLUDE_UNAVAILABLE",
        },
        allowed_capabilities={"search_products"},
        context=context,
    )
    cdf_budget = await executor.execute(
        requested_name="search_products",
        model_arguments={
            "max_budget": "160000.00",
            "budget_currency": "CDF",
            "search_mode": "INCLUDE_UNAVAILABLE",
        },
        allowed_capabilities={"search_products"},
        context=context,
    )
    detail = await executor.execute(
        requested_name="get_product_details",
        model_arguments={"sellable_item_id": str(item_8l.sellable_item_id)},
        allowed_capabilities={"get_product_details"},
        context=context,
    )

    assert isinstance(sellable_only, CapabilitySuccess)
    assert [item.sellable_item_id for item in sellable_only.output.items] == [
        item_6l.sellable_item_id
    ]
    assert sellable_only.output.items[0].primary_media.media_id == product_media.media_id
    assert sellable_only.output.items[0].primary_media.source_scope == "product"

    assert isinstance(include_unavailable, CapabilitySuccess)
    assert [item.sellable_item_id for item in include_unavailable.output.items] == [
        item_6l.sellable_item_id,
        item_8l.sellable_item_id,
    ]
    assert include_unavailable.output.items[1].offer_status == "out_of_stock"
    assert include_unavailable.output.items[1].is_sellable_now is False
    assert include_unavailable.output.items[1].primary_media.media_id == item_media.media_id
    assert include_unavailable.output.items[1].primary_media.source_scope == (
        "sellable_item"
    )
    assert "asset_url" not in str(include_unavailable.output.model_dump(mode="json"))

    assert isinstance(usd_budget, CapabilitySuccess)
    assert [item.sellable_item_id for item in usd_budget.output.items] == [
        item_6l.sellable_item_id
    ]
    assert isinstance(cdf_budget, CapabilitySuccess)
    assert [item.sellable_item_id for item in cdf_budget.output.items] == [
        item_6l.sellable_item_id
    ]
    assert cdf_budget.output.items[0].derived_cdf_quote.amount == Decimal("154000.00")

    assert isinstance(detail, CapabilitySuccess)
    assert detail.output.product.sellable_item_id == item_8l.sellable_item_id
    assert detail.output.product.offer_status == "out_of_stock"
    assert detail.output.product.availability == "out_of_stock"
    assert detail.output.product.primary_media.media_id == item_media.media_id
    assert await _snapshot(factory) == before_reads

    async with factory() as session:
        await session.execute(
            text(
                "UPDATE mbb.exchange_rates SET ended_at = NOW() "
                "WHERE ended_at IS NULL"
            )
        )
        await session.commit()
    before_missing_fx = await _snapshot(factory)

    missing_fx = await executor.execute(
        requested_name="search_products",
        model_arguments={
            "max_budget": "160000.00",
            "budget_currency": "CDF",
        },
        allowed_capabilities={"search_products"},
        context=context,
    )

    assert missing_fx == CapabilityFailure(
        CapabilityErrorCategory.execution_failed,
        safe_code="cdf_quote_unavailable",
    )
    assert await _snapshot(factory) == before_missing_fx
