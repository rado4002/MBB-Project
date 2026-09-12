from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.adapters.fx.exchange_rate_api import (
    ExchangeRateAPIError,
    ExchangeRateAPIObservation,
)
from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.inventory import InventoryRecord
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.models.order_draft import OrderDraft
from app.models.payment import Payment
from app.models.pricing import ExchangeRate, SellableItemPrice
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.m7_conversion.order_drafts import prepare_order_draft
from app.modules.pricing.service import (
    AUTOMATIC,
    MANUAL,
    AutomaticExchangeRateRejected,
    ExchangeRateAuthorityUnavailable,
    get_active_usd_cdf_rate,
    refresh_automatic_exchange_rate,
    set_current_exchange_rate,
    set_exchange_rate_authority_mode,
)
from app.modules.product_offer.service import get_product_offer

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2B_TEST_DATABASE_URL is required for exchange-rate authority evidence",
)

TRUNCATE = text(
    """
    TRUNCATE TABLE
        mbb.order_drafts,
        mbb.messages,
        mbb.conversations,
        mbb.customers,
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


class FakeProvider:
    def __init__(self, observation: ExchangeRateAPIObservation) -> None:
        self.observation = observation
        self.calls = 0

    async def fetch_usd_cdf(self) -> ExchangeRateAPIObservation:
        self.calls += 1
        return self.observation


class FailingProvider:
    async def fetch_usd_cdf(self) -> ExchangeRateAPIObservation:
        raise ExchangeRateAPIError("provider_unavailable")


def _admin_context(admin: OperatorAccount, suffix: str) -> CommerceAdminContext:
    return CommerceAdminContext(admin.account_id, f"fx-authority-{suffix}")


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL)
    async with database_engine.begin() as connection:
        await connection.execute(TRUNCATE)
        await connection.execute(
            text(
                "UPDATE mbb.exchange_rate_authorities "
                "SET mode = 'MANUAL', revision = 1, updated_at = NOW(), "
                "updated_by_account_id = NULL "
                "WHERE base_currency = 'USD' AND quote_currency = 'CDF'"
            )
        )
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(TRUNCATE)
            await connection.execute(
                text(
                    "UPDATE mbb.exchange_rate_authorities "
                    "SET mode = 'MANUAL', revision = 1, updated_at = NOW(), "
                    "updated_by_account_id = NULL "
                    "WHERE base_currency = 'USD' AND quote_currency = 'CDF'"
                )
            )
        await database_engine.dispose()


async def _seed(factory):
    now = datetime.now(timezone.utc)
    admin = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="fx.authority.admin",
        display_name="FX Authority Admin",
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
    customer = Customer(
        phone_number="+243812345678",
        name="FX Draft Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        start_time=now,
        last_message_time=now,
        status="qualifying",
        language_detected="french",
        context={},
        message_count=1,
        owner_type="ai",
        human_owner_account_id=None,
        ai_execution_state="eligible",
        ownership_version=1,
        ownership_updated_at=now,
        created_at=now,
        updated_at=now,
    )
    product = Product(
        product_id=uuid.uuid4(),
        name="Authority Air Fryer",
        category_code="air_fryer",
        description="Isolated automatic FX fixture.",
        active=True,
        created_at=now,
        updated_at=now,
    )
    item = SellableItem(
        sellable_item_id=uuid.uuid4(),
        product_id=product.product_id,
        model_label="6L",
        sku="FX-AUTHORITY-6L",
        attributes={"capacity_l": 6},
        active=True,
        created_at=now,
        updated_at=now,
    )
    price = SellableItemPrice(
        sellable_item_id=item.sellable_item_id,
        amount=Decimal("55.00"),
        currency="USD",
        effective_at=now,
        ended_at=None,
    )
    inventory = InventoryRecord(
        sellable_item_id=item.sellable_item_id,
        status="available",
        updated_at=now,
    )
    inbound = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        timestamp=now,
        direction="inbound",
        content="Je prends deux.",
        content_type="text",
        language="french",
        whatsapp_message_id=f"fx-{uuid.uuid4()}",
        created_at=now,
    )
    async with factory() as session:
        session.add_all(
            [admin, customer, conversation, product, item, price, inventory, inbound]
        )
        await session.commit()
    return admin, conversation, item, inbound


@pytest.mark.asyncio
async def test_explicit_mode_switch_controls_offer_and_draft_provenance(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin, conversation, item, inbound = await _seed(factory)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        manual = await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin_context(admin, "manual"),
            now=now,
        )
        automatic = await refresh_automatic_exchange_rate(
            session,
            provider=FakeProvider(
                ExchangeRateAPIObservation(
                    base_currency="USD",
                    quote_currency="CDF",
                    rate=Decimal("3000.000000"),
                    published_at=now - timedelta(hours=1),
                )
            ),
            administrator=_admin_context(admin, "refresh"),
            now=now,
        )
        await session.commit()

    async with factory() as session:
        active = await get_active_usd_cdf_rate(session, at=now)
        offer = await get_product_offer(session, item.sellable_item_id, read_at=now)
        assert active is not None and active.exchange_rate_id == manual.exchange_rate_id
        assert offer is not None and offer.derived_cdf_quote is not None
        assert offer.derived_cdf_quote.cdf_amount == Decimal("154000.00")

        authority = await set_exchange_rate_authority_mode(
            session,
            mode=AUTOMATIC,
            administrator=_admin_context(admin, "automatic"),
            now=now,
        )
        assert authority.mode == AUTOMATIC and authority.revision == 2
        await session.commit()

    async with factory() as session:
        offer = await get_product_offer(session, item.sellable_item_id, read_at=now)
        assert offer is not None and offer.derived_cdf_quote is not None
        assert offer.derived_cdf_quote.cdf_amount == Decimal("165000.00")
        assert offer.derived_cdf_quote.exchange_rate_id == automatic.exchange_rate_id
        prepared = await prepare_order_draft(
            session,
            conversation_id=conversation.conversation_id,
            source_message_id=inbound.message_id,
            turn_id=uuid.uuid4(),
            expected_ownership_version=1,
            expected_commercial_state_revision=0,
            sellable_item_id=item.sellable_item_id,
            quantity=2,
        )
        await session.commit()

    async with factory() as session:
        draft = await session.scalar(
            select(OrderDraft).where(OrderDraft.draft_id == prepared.draft_id)
        )
        rate = await session.get(
            ExchangeRate, draft.exchange_rate_id if draft else None
        )
        assert draft is not None
        assert draft.exchange_rate_id == automatic.exchange_rate_id
        assert rate is not None and rate.source == "EXCHANGE_RATE_API"
        assert rate.published_at == automatic.published_at
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0

        stale_offer = await get_product_offer(
            session,
            item.sellable_item_id,
            read_at=now + timedelta(hours=36, seconds=1),
        )
        assert stale_offer is not None
        assert stale_offer.derived_cdf_quote is None
        assert stale_offer.cdf_quote_unavailable_reason == "current_fx_unavailable"

        authority = await set_exchange_rate_authority_mode(
            session,
            mode=MANUAL,
            administrator=_admin_context(admin, "manual-again"),
            now=now + timedelta(hours=37),
        )
        assert authority.mode == MANUAL and authority.revision == 3
        await session.commit()

    async with factory() as session:
        restored = await get_product_offer(
            session,
            item.sellable_item_id,
            read_at=now + timedelta(hours=37),
        )
        assert restored is not None and restored.derived_cdf_quote is not None
        assert restored.derived_cdf_quote.exchange_rate_id == manual.exchange_rate_id
        assert restored.derived_cdf_quote.cdf_amount == Decimal("154000.00")


@pytest.mark.asyncio
async def test_stale_invalid_and_failed_provider_data_never_becomes_active(
    engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    admin, _conversation, _item, _inbound = await _seed(factory)
    now = datetime.now(timezone.utc)

    async with factory() as session:
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin_context(admin, "manual"),
            now=now,
        )
        await session.commit()

    observations = [
        ExchangeRateAPIObservation(
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("3000.000000"),
            published_at=now - timedelta(hours=36, seconds=1),
        ),
        ExchangeRateAPIObservation(
            base_currency="EUR",
            quote_currency="CDF",
            rate=Decimal("3000.000000"),
            published_at=now,
        ),
        ExchangeRateAPIObservation(
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("3000.000000"),
            published_at=now + timedelta(seconds=1),
        ),
        ExchangeRateAPIObservation(
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("0"),
            published_at=now,
        ),
        ExchangeRateAPIObservation(
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("3000.0000001"),
            published_at=now,
        ),
    ]
    async with factory() as session:
        for observation in observations:
            with pytest.raises(AutomaticExchangeRateRejected):
                await refresh_automatic_exchange_rate(
                    session,
                    provider=FakeProvider(observation),
                    administrator=_admin_context(admin, "rejected"),
                    now=now,
                )
            await session.rollback()
        with pytest.raises(ExchangeRateAPIError):
            await refresh_automatic_exchange_rate(
                session,
                provider=FailingProvider(),
                administrator=_admin_context(admin, "failure"),
                now=now,
            )
        await session.rollback()
        assert (
            await session.scalar(
                select(func.count())
                .select_from(ExchangeRate)
                .where(ExchangeRate.authority_mode == AUTOMATIC)
            )
            == 0
        )
        with pytest.raises(ExchangeRateAuthorityUnavailable):
            await set_exchange_rate_authority_mode(
                session,
                mode=AUTOMATIC,
                administrator=_admin_context(admin, "unavailable"),
                now=now,
            )
        await session.rollback()
        active = await get_active_usd_cdf_rate(session, at=now)
        assert active is not None and active.authority_mode == MANUAL
