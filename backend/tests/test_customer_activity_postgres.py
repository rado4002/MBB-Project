from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi import Response
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.api.browser_auth_deps import BrowserPrincipal, BrowserSessionContext
from app.api.v1.operator_conversations import get_operator_customer_activity
from app.config import Settings
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.operator_identity.browser_auth import BrowserAuthState

DATABASE_URL = os.environ.get("E1_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="E1_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


def _principal() -> BrowserPrincipal:
    now = datetime.now(timezone.utc)
    account = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="activity.operator",
        display_name="Activity Operator",
        email_normalized=None,
        password_hash="not-used-for-query-evidence",
        role="operator",
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )
    state = BrowserAuthState(
        redis_client=object(),
        settings=Settings(
            browser_session_hmac_secret="s" * 32,
            browser_csrf_hmac_secret="c" * 32,
        ),
    )
    return BrowserPrincipal(
        account=account,
        session=BrowserSessionContext(
            raw_token="not-used",
            record=object(),
            state=state,
        ),
        capabilities=frozenset({"conversation.read", "order.read"}),
    )


@pytest.mark.asyncio
async def test_customer_activity_is_bounded_and_customer_isolated() -> None:
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    base_time = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
    customer_phone = "+243810000901"
    other_phone = "+243810000902"
    conversation_ids = [uuid.uuid4() for _ in range(6)]
    other_conversation_id = uuid.uuid4()
    try:
        async with engine.connect() as connection:
            transaction = await connection.begin()
            session = AsyncSession(bind=connection, expire_on_commit=False)
            try:
                session.add_all(
                    [
                        Customer(
                            phone_number=customer_phone,
                            name="Activity Customer",
                            city="Kinshasa",
                            preferred_language="french",
                            opt_out_flag=False,
                            consent_given=True,
                        ),
                        Customer(
                            phone_number=other_phone,
                            name="Other Customer",
                            city="Goma",
                            preferred_language="swahili",
                        ),
                    ]
                )
                await session.flush()

                conversations = [
                    Conversation(
                        conversation_id=conversation_id,
                        customer_id=customer_phone,
                        last_message_time=base_time - timedelta(hours=index),
                        status="active",
                        language_detected="french",
                    )
                    for index, conversation_id in enumerate(conversation_ids)
                ]
                conversations.append(
                    Conversation(
                        conversation_id=other_conversation_id,
                        customer_id=other_phone,
                        last_message_time=base_time + timedelta(days=1),
                        status="active",
                        language_detected="swahili",
                    )
                )
                session.add_all(conversations)
                await session.flush()

                lead_ids = [uuid.uuid4() for _ in range(6)]
                session.add_all(
                    [
                        Lead(
                            lead_id=lead_id,
                            customer_id=customer_phone,
                            conversation_id=conversation_ids[index],
                            score="warm",
                            score_value=5,
                            stage="consideration",
                            intent="product_inquiry",
                            product_interest=[],
                            source=(
                                "referral_partner" if index == 0 else "social_campaign"
                            ),
                        )
                        for index, lead_id in enumerate(lead_ids)
                    ]
                )
                other_lead_id = uuid.uuid4()
                session.add(
                    Lead(
                        lead_id=other_lead_id,
                        customer_id=other_phone,
                        conversation_id=other_conversation_id,
                        score="cold",
                        score_value=10,
                        stage="awareness",
                        intent="support",
                        product_interest=[],
                        source="other_customer_source",
                    )
                )
                session.add(
                    Message(
                        conversation_id=conversation_ids[0],
                        timestamp=base_time,
                        direction="inbound",
                        inbound_receipt_order=1,
                        content="Verified WhatsApp message",
                        content_type="text",
                        language="french",
                        whatsapp_message_id=f"activity-{uuid.uuid4()}",
                    )
                )
                await session.flush()

                for index, lead_id in enumerate(lead_ids):
                    session.add(
                        Order(
                            lead_id=lead_id,
                            customer_id=customer_phone,
                            items=[{"product_id": f"activity-{index}"}],
                            total_amount=Decimal("1000.00") + index,
                            currency="CDF",
                            payment_type="cod",
                            delivery_zone="Kinshasa",
                            delivery_method="moto_taxi",
                            status="pending",
                            created_at=base_time - timedelta(hours=index),
                        )
                    )
                other_order_id = uuid.uuid4()
                session.add(
                    Order(
                        order_id=other_order_id,
                        lead_id=other_lead_id,
                        customer_id=other_phone,
                        items=[{"product_id": "other"}],
                        total_amount=Decimal("2000.00"),
                        currency="CDF",
                        payment_type="cod",
                        delivery_zone="Goma",
                        delivery_method="moto_taxi",
                        status="pending",
                        created_at=base_time + timedelta(days=1),
                    )
                )
                await session.flush()

                result = await get_operator_customer_activity(
                    conversation_id=conversation_ids[0],
                    response=Response(),
                    principal=_principal(),
                    _order_principal=_principal(),
                    db=session,
                )

                assert result.customer.phone_masked == "***0901"
                assert result.customer.city == "Kinshasa"
                assert result.customer.consent_given is True
                assert len(result.conversations) == 5
                assert [item.conversation_id for item in result.conversations] == (
                    conversation_ids[:5]
                )
                assert result.conversations[0].communication_channel == "whatsapp"
                assert result.conversations[0].acquisition_source == "referral_partner"
                assert result.conversations[1].communication_channel is None
                assert result.conversations[1].acquisition_source == "social_campaign"
                assert all(
                    item.conversation_id != other_conversation_id
                    for item in result.conversations
                )
                assert len(result.orders) == 5
                assert all(item.order_id != other_order_id for item in result.orders)
            finally:
                await session.close()
                await transaction.rollback()
    finally:
        await engine.dispose()
