from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityExecutor,
    CapabilitySuccess,
    TrustedCapabilityContext,
)
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.modules.m1_gateway.service import process_inbound
from app.modules.m4_conversation.ai_handoff import (
    StaleAIAuthority,
    request_human_handoff,
)
from app.modules.m4_conversation.ownership import (
    ReturnToAIDisabled,
    ai_reply_ownership_version,
    transition_ownership,
)

DATABASE_URL = os.environ.get("AI1E_TEST_DATABASE_URL")
IDEMPOTENCY_SECRET = "ai-handoff-ownership-secret-" + ("x" * 32)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI1E_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
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


async def _seed(engine: AsyncEngine) -> tuple[OperatorAccount, Conversation]:
    now = datetime(2026, 8, 11, 10, tzinfo=timezone.utc)
    account = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="ai.handoff.operator",
        display_name="AI Handoff Operator",
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
    customer = Customer(
        phone_number="+243810000081",
        name="AI Handoff Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        status="qualifying",
        language_detected="french",
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
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all([account, customer, conversation])
        await session.commit()
    return account, conversation


async def _handoff(
    engine: AsyncEngine,
    conversation_id: uuid.UUID,
    expected_version: int,
):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        return await request_human_handoff(
            session,
            conversation_id=conversation_id,
            expected_ownership_version=expected_version,
        )


async def _transition(
    engine: AsyncEngine,
    *,
    account: OperatorAccount,
    conversation_id: uuid.UUID,
    target: str,
    expected_version: int,
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
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai1e-postgres",
            ai_adapter=ai_adapter,
        )


@pytest.mark.asyncio
async def test_full_authority_cycle_duplicate_stale_and_repeat_handoff(
    engine: AsyncEngine,
) -> None:
    account, conversation = await _seed(engine)
    first = await _handoff(engine, conversation.conversation_id, 1)
    duplicate = await _handoff(engine, conversation.conversation_id, 1)

    assert first.replayed is False
    assert duplicate.replayed is True
    assert duplicate.escalation_ticket_id == first.escalation_ticket_id
    assert duplicate.ownership_version == 2

    takeover = await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=2,
    )
    assert takeover.ownership.owner_type == "human"
    assert takeover.ownership.human_owner_account_id == account.account_id
    assert takeover.ownership.version == 3

    returned = await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="ai",
        expected_version=3,
    )
    assert returned.ownership.owner_type == "ai"
    assert returned.ownership.human_owner_account_id is None
    assert returned.ownership.ai_execution_state == "eligible"
    assert returned.ownership.version == 4

    with pytest.raises(StaleAIAuthority):
        await _handoff(engine, conversation.conversation_id, 1)

    second = await _handoff(engine, conversation.conversation_id, 4)
    assert second.escalation_ticket_id != first.escalation_ticket_id
    assert second.ownership_version == 5

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        tickets = (
            await session.scalars(
                select(EscalationTicket)
                .where(EscalationTicket.conversation_id == conversation.conversation_id)
                .order_by(EscalationTicket.created_at, EscalationTicket.ticket_id)
            )
        ).all()
        assert persisted is not None
        assert persisted.status == "qualifying"
        assert persisted.owner_type == "ai"
        assert persisted.human_owner_account_id is None
        assert persisted.ai_execution_state == "paused"
        assert persisted.ownership_version == 5
        assert len(tickets) == 2
        assert tickets[0].status == "closed"
        assert tickets[1].status == "open"
        assert all(ticket.source == "ai_capability" for ticket in tickets)
        assert all(ticket.reason == "human_handoff" for ticket in tickets)
        assert await session.scalar(
            select(func.count())
            .select_from(EscalationTicket)
            .where(EscalationTicket.status.in_(("open", "in_progress")))
        ) == 1


@pytest.mark.asyncio
async def test_concurrent_duplicate_handoffs_apply_one_transition(
    engine: AsyncEngine,
) -> None:
    _account, conversation = await _seed(engine)
    first, second = await asyncio.gather(
        _handoff(engine, conversation.conversation_id, 1),
        _handoff(engine, conversation.conversation_id, 1),
    )

    assert sorted((first.replayed, second.replayed)) == [False, True]
    assert first.escalation_ticket_id == second.escalation_ticket_id
    assert first.ownership_version == second.ownership_version == 2
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        assert persisted is not None
        assert persisted.ai_execution_state == "paused"
        assert persisted.ownership_version == 2
        assert await session.scalar(
            select(func.count()).select_from(EscalationTicket)
        ) == 1


@pytest.mark.asyncio
async def test_non_ai_attention_is_reused_without_lifecycle_rewrite(
    engine: AsyncEngine,
) -> None:
    account, conversation = await _seed(engine)
    ticket_id = uuid.uuid4()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            EscalationTicket(
                ticket_id=ticket_id,
                conversation_id=conversation.conversation_id,
                customer_id=conversation.customer_id,
                priority="high",
                reason="complex_complaint",
                source="operator_browser",
                escalation_type="complex_issue",
                operator_reason="Customer needs a Human review.",
                created_by_account_id=account.account_id,
                status="open",
                transcript_snapshot=[{"preserved": True}],
            )
        )
        await session.commit()

    handoff = await _handoff(engine, conversation.conversation_id, 1)
    assert handoff.escalation_ticket_id == ticket_id
    assert handoff.escalation_source == "operator_browser"
    assert handoff.ownership_version == 2

    await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=2,
    )
    await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="ai",
        expected_version=3,
    )

    async with factory() as session:
        ticket = await session.get(EscalationTicket, ticket_id)
        assert ticket is not None
        assert ticket.status == "open"
        assert ticket.source == "operator_browser"
        assert ticket.reason == "complex_complaint"
        assert ticket.escalation_type == "complex_issue"
        assert ticket.created_by_account_id == account.account_id
        assert ticket.transcript_snapshot == [{"preserved": True}]
        assert await session.scalar(
            select(func.count()).select_from(EscalationTicket)
        ) == 1


@pytest.mark.asyncio
async def test_disabled_return_preserves_human_authority_and_ai_ticket(
    engine: AsyncEngine,
) -> None:
    account, conversation = await _seed(engine)
    handoff = await _handoff(engine, conversation.conversation_id, 1)
    await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=2,
    )

    with pytest.raises(ReturnToAIDisabled):
        await _transition(
            engine,
            account=account,
            conversation_id=conversation.conversation_id,
            target="ai",
            expected_version=3,
            ai_adapter="disabled",
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        ticket = await session.get(EscalationTicket, handoff.escalation_ticket_id)
        assert persisted is not None
        assert persisted.owner_type == "human"
        assert persisted.human_owner_account_id == account.account_id
        assert persisted.ownership_version == 3
        assert ticket is not None and ticket.status == "in_progress"


@pytest.mark.asyncio
async def test_waiting_state_accepts_deduplicates_and_attaches_inbound(
    engine: AsyncEngine,
) -> None:
    _account, conversation = await _seed(engine)
    await _handoff(engine, conversation.conversation_id, 1)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    first_message_id = uuid.uuid4()

    async with factory() as session:
        first = await process_inbound(
            session=session,
            customer_phone=conversation.customer_id,
            content="Je veux ajouter une precision.",
            content_type="text",
            timestamp=datetime(2026, 8, 11, 10, 5, tzinfo=timezone.utc),
            whatsapp_message_id="waiting-inbound-1",
            message_id=first_message_id,
        )
        await session.commit()
        assert first.conversation_id == conversation.conversation_id
        assert first.is_duplicate is False

    async with factory() as session:
        duplicate = await process_inbound(
            session=session,
            customer_phone=conversation.customer_id,
            content="duplicate delivery",
            content_type="text",
            timestamp=datetime(2026, 8, 11, 10, 6, tzinfo=timezone.utc),
            whatsapp_message_id="waiting-inbound-1",
            message_id=uuid.uuid4(),
        )
        await session.rollback()
        assert duplicate.is_duplicate is True
        assert duplicate.existing_message_id == first_message_id
        assert duplicate.conversation_id == conversation.conversation_id

    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        messages = (
            await session.scalars(
                select(Message).where(
                    Message.conversation_id == conversation.conversation_id
                )
            )
        ).all()
        assert persisted is not None
        assert persisted.owner_type == "ai"
        assert persisted.human_owner_account_id is None
        assert persisted.ai_execution_state == "paused"
        assert persisted.status == "qualifying"
        assert len(messages) == 1
        assert messages[0].content == "Je veux ajouter une precision."
        assert await ai_reply_ownership_version(
            session, conversation.conversation_id
        ) is None
        assert await session.scalar(
            select(func.count())
            .select_from(EscalationTicket)
            .where(
                EscalationTicket.conversation_id == conversation.conversation_id,
                EscalationTicket.status.in_(("open", "in_progress")),
            )
        ) == 1


@pytest.mark.asyncio
async def test_stale_ai_generation_cannot_persist_or_send_after_complete_cycle(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.adapters as adapters
    import app.database as database
    from app.i18n.messages import t
    from app.tasks import m1

    account, conversation = await _seed(engine)
    await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="human",
        expected_version=1,
    )
    await _transition(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        target="ai",
        expected_version=2,
    )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    stale_fallback = t("error_fallback", "french")
    persisted = await m1._persist_outbound(
        conversation_id=conversation.conversation_id,
        content=stale_fallback,
        language="french",
        processing_time_ms=10,
        expected_ownership_version=1,
    )
    assert persisted is None

    monkeypatch.setattr(
        m1.settings,
        "whatsapp_send_enabled",
        True,
    )
    monkeypatch.setattr(
        adapters,
        "get_messaging_adapter",
        lambda: pytest.fail("stale AI work reached the messaging adapter"),
    )
    sent = await m1._send_safe(
        "+243810000081",
        stale_fallback,
        idempotency_key=str(uuid.uuid4()),
        conversation_id=conversation.conversation_id,
        expected_ownership_version=1,
    )
    assert sent == {"status": "skipped"}

    async with factory() as session:
        persisted_conversation = await session.get(
            Conversation, conversation.conversation_id
        )
        assert persisted_conversation is not None
        assert persisted_conversation.owner_type == "ai"
        assert persisted_conversation.ai_execution_state == "eligible"
        assert persisted_conversation.ownership_version == 3
        assert await session.scalar(
            select(func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation.conversation_id)
        ) == 0


@pytest.mark.asyncio
async def test_registered_capability_executes_with_only_trusted_authority_scope(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.database as database

    _account, conversation = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
        requested_name="request_human_handoff",
        model_arguments={"reason_category": "customer_requested_human"},
        allowed_capabilities={"request_human_handoff"},
        context=TrustedCapabilityContext(
            conversation_id=conversation.conversation_id,
            turn_id=uuid.uuid4(),
            expected_ownership_version=1,
        ),
    )

    assert isinstance(result, CapabilitySuccess)
    assert result.output.state == "waiting_for_human"
    assert result.output.ownership_version == 2
    async with factory() as session:
        persisted = await session.get(Conversation, conversation.conversation_id)
        assert persisted is not None
        assert persisted.owner_type == "ai"
        assert persisted.human_owner_account_id is None
        assert persisted.ai_execution_state == "paused"
