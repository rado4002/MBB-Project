from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.models.conversation import Conversation
from app.models.conversation_ownership_idempotency import (
    ConversationOwnershipIdempotency,
)
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import OperatorAuditEvent
from app.modules.m4_conversation.ownership import (
    OwnershipConflict,
    ReturnToAIBlocked,
    ReturnToAIDisabled,
    ReturnToAIUnavailable,
    transition_ownership,
)

DATABASE_URL = os.environ.get("E2_TEST_DATABASE_URL")
IDEMPOTENCY_SECRET = "ownership-test-secret-" + ("x" * 32)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="E2_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL, pool_size=10)
    truncate = text(
        """
        TRUNCATE TABLE
            mbb.conversation_ownership_idempotency,
            mbb.operator_audit_security_metadata,
            mbb.operator_audit_events,
            mbb.escalation_tickets,
            mbb.messages,
            mbb.conversations,
            mbb.customers,
            mbb.operator_accounts
        RESTART IDENTITY CASCADE
        """
    )
    async with database_engine.begin() as connection:
        await connection.execute(truncate)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(truncate)
        await database_engine.dispose()


async def _seed(
    engine: AsyncEngine,
) -> tuple[list[OperatorAccount], Conversation, Message]:
    now = datetime(2026, 8, 4, 10, tzinfo=timezone.utc)
    accounts = [
        OperatorAccount(
            account_id=uuid.uuid4(),
            username_normalized=f"owner.{index}",
            display_name=f"Owner {index}",
            email_normalized=None,
            password_hash="not-used",
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
        for index in range(2)
    ]
    customer = Customer(
        phone_number="+243810000001",
        name="Ownership Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="qualifying",
        language_detected="french",
        context={"preserve": True},
        message_count=1,
        owner_type="ai",
        human_owner_account_id=None,
        ai_execution_state="eligible",
        ownership_version=1,
        ownership_updated_at=now,
        start_time=now,
        last_message_time=now,
        created_at=now,
        updated_at=now,
    )
    message = Message(
        message_id=uuid.uuid4(),
        conversation_id=conversation.conversation_id,
        whatsapp_message_id="ownership-message",
        direction="inbound",
        content="Existing customer data remains unchanged.",
        content_type="text",
        language="french",
        timestamp=now,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([*accounts, customer, conversation, message])
        await session.commit()
    return accounts, conversation, message


async def _transition(
    engine: AsyncEngine,
    *,
    account: OperatorAccount,
    conversation_id: uuid.UUID,
    target: str,
    expected_version: int,
    key: uuid.UUID,
    ai_adapter: str = "claude",
):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        return await transition_ownership(
            session,
            conversation_id=conversation_id,
            target_owner_type=target,
            expected_version=expected_version,
            actor_account_id=account.account_id,
            actor_display_name=account.display_name,
            actor_role=account.role,
            idempotency_key=key,
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ownership-postgres",
            ai_adapter=ai_adapter,
            source_network_fingerprint=None,
            user_agent_fingerprint=None,
        )


@pytest.mark.asyncio
async def test_takeover_is_atomic_durable_audited_and_exactly_replayable(
    engine: AsyncEngine,
) -> None:
    accounts, conversation, seeded_message = await _seed(engine)
    key = uuid.uuid4()
    first = await _transition(
        engine,
        account=accounts[0],
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=1,
        key=key,
    )
    replay = await _transition(
        engine,
        account=accounts[0],
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=1,
        key=key,
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert first.ownership.owner_type == "human"
    assert first.ownership.human_owner_account_id == accounts[0].account_id
    assert first.ownership.ai_execution_state == "paused"
    assert first.ownership.version == 2

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        message = await session.get(Message, seeded_message.message_id)
        audit = await session.scalar(select(OperatorAuditEvent))
        assert persisted is not None
        assert persisted.status == "qualifying"
        assert persisted.context == {"preserve": True}
        assert persisted.owner_type == "human"
        assert persisted.human_owner_account_id == accounts[0].account_id
        assert persisted.ai_execution_state == "paused"
        assert message is not None
        assert message.content == "Existing customer data remains unchanged."
        assert audit is not None
        assert audit.action == "conversation_taken_over"
        assert audit.target_id == str(conversation.conversation_id)
        assert audit.event_metadata["ownership_version"] == 2
        assert await session.scalar(
            select(func.count()).select_from(OperatorAuditEvent)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(
                ConversationOwnershipIdempotency
            )
        ) == 1


@pytest.mark.asyncio
async def test_concurrent_takeovers_choose_exactly_one_owner(
    engine: AsyncEngine,
) -> None:
    accounts, conversation, _message = await _seed(engine)

    async def attempt(account: OperatorAccount):
        try:
            result = await _transition(
                engine,
                account=account,
                conversation_id=conversation.conversation_id,
                target="human",
                expected_version=1,
                key=uuid.uuid4(),
            )
            return ("won", result.ownership.human_owner_account_id)
        except OwnershipConflict as exc:
            return ("conflict", exc.current.human_owner_account_id)

    results = await asyncio.gather(*(attempt(account) for account in accounts))
    assert sorted(result[0] for result in results) == ["conflict", "won"]
    winning_ids = {result[1] for result in results}
    assert len(winning_ids) == 1
    assert next(iter(winning_ids)) in {account.account_id for account in accounts}

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert await session.scalar(
            select(func.count()).select_from(OperatorAuditEvent)
        ) == 1
        assert await session.scalar(
            select(func.count()).select_from(
                ConversationOwnershipIdempotency
            )
        ) == 1


@pytest.mark.asyncio
async def test_stale_and_disabled_returns_fail_closed_before_eligible_return(
    engine: AsyncEngine,
) -> None:
    accounts, conversation, _message = await _seed(engine)
    await _transition(
        engine,
        account=accounts[0],
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=1,
        key=uuid.uuid4(),
    )

    with pytest.raises(OwnershipConflict):
        await _transition(
            engine,
            account=accounts[0],
            conversation_id=conversation.conversation_id,
            target="ai",
            expected_version=1,
            key=uuid.uuid4(),
        )
    with pytest.raises(ReturnToAIDisabled):
        await _transition(
            engine,
            account=accounts[0],
            conversation_id=conversation.conversation_id,
            target="ai",
            expected_version=2,
            key=uuid.uuid4(),
            ai_adapter="disabled",
        )
    with pytest.raises(ReturnToAIUnavailable):
        await _transition(
            engine,
            account=accounts[0],
            conversation_id=conversation.conversation_id,
            target="ai",
            expected_version=2,
            key=uuid.uuid4(),
            ai_adapter="unknown",
        )

    returned = await _transition(
        engine,
        account=accounts[0],
        conversation_id=conversation.conversation_id,
        target="ai",
        expected_version=2,
        key=uuid.uuid4(),
        ai_adapter="claude",
    )
    assert returned.ownership.owner_type == "ai"
    assert returned.ownership.human_owner_account_id is None
    assert returned.ownership.ai_execution_state == "eligible"
    assert returned.ownership.version == 3


@pytest.mark.asyncio
async def test_unresolved_advanced_ticket_blocks_return_without_changing_owner(
    engine: AsyncEngine,
) -> None:
    accounts, conversation, _message = await _seed(engine)
    await _transition(
        engine,
        account=accounts[0],
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=1,
        key=uuid.uuid4(),
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EscalationTicket(
                conversation_id=conversation.conversation_id,
                customer_id=conversation.customer_id,
                priority="medium",
                reason="complex_complaint",
                status="open",
                transcript_snapshot=[],
            )
        )
        await session.commit()

    with pytest.raises(ReturnToAIBlocked):
        await _transition(
            engine,
            account=accounts[0],
            conversation_id=conversation.conversation_id,
            target="ai",
            expected_version=2,
            key=uuid.uuid4(),
        )
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        assert persisted is not None
        assert persisted.owner_type == "human"
        assert persisted.human_owner_account_id == accounts[0].account_id
        assert persisted.ai_execution_state == "paused"
