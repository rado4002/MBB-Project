from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    async_sessionmaker,
    create_async_engine,
)

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import (
    OperatorAuditEvent,
    OperatorAuditSecurityMetadata,
)
from app.models.operator_escalation_idempotency import (
    OperatorEscalationIdempotency,
)
from app.modules.m8_maps import operator_escalation as service
from app.modules.m8_maps.escalation import create_ticket as create_legacy_ticket
from app.modules.m8_maps.operator_escalation import (
    EscalationAlreadyOpen,
    IdempotencyConflict,
    IdempotencyInProgress,
    create_operator_escalation,
)

DATABASE_URL = os.environ.get("E2_TEST_DATABASE_URL")
IDEMPOTENCY_SECRET = "e2-idempotency-secret-" + ("x" * 32)
REASON = "Customer requested a specialist review of a complex issue."

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="E2_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL, pool_size=10)
    async with database_engine.begin() as connection:
        await connection.execute(
            text(
                """
                TRUNCATE TABLE
                    mbb.operator_escalation_idempotency,
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
        )
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(
                text(
                    """
                    TRUNCATE TABLE
                        mbb.operator_escalation_idempotency,
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
            )
        await database_engine.dispose()


async def _seed(
    engine: AsyncEngine,
    *,
    conversations: int = 1,
) -> tuple[OperatorAccount, list[Conversation]]:
    factory = async_sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 8, 3, 12, tzinfo=timezone.utc)
    account = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="e2.operator",
        display_name="E2 Operator",
        email_normalized=None,
        password_hash="not-used-in-service-test",
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
    seeded: list[Conversation] = []
    async with factory() as session:
        session.add(account)
        for index in range(conversations):
            phone = f"+2438100000{index:02d}"
            customer = Customer(
                phone_number=phone,
                name=f"E2 Customer {index}",
                city="Kinshasa",
                preferred_language="french",
            )
            conversation = Conversation(
                conversation_id=uuid.uuid4(),
                customer_id=phone,
                status="qualifying",
                language_detected="french",
                context={"automation_state": "unchanged", "handoff": "bot"},
                message_count=1,
                start_time=now,
                last_message_time=now,
                created_at=now,
                updated_at=now,
            )
            session.add_all([customer, conversation])
            session.add(
                Message(
                    message_id=uuid.uuid4(),
                    conversation_id=conversation.conversation_id,
                    whatsapp_message_id=f"e2-message-{index}",
                    direction="inbound",
                    content="Authoritative conversation content must not be copied.",
                    content_type="text",
                    language="french",
                    timestamp=now,
                )
            )
            seeded.append(conversation)
        await session.commit()
    return account, seeded


async def _create(
    engine: AsyncEngine,
    *,
    account: OperatorAccount,
    conversation_id: uuid.UUID,
    key: uuid.UUID,
    reason: str = REASON,
    escalation_type: str = "complex_issue",
    priority: str = "medium",
):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        return await create_operator_escalation(
            session,
            conversation_id=conversation_id,
            reason=reason,
            escalation_type=escalation_type,
            priority=priority,
            actor_account_id=account.account_id,
            actor_display_name=account.display_name,
            actor_role=account.role,
            idempotency_key=key,
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="e2-postgres-request",
            source_network_fingerprint="a" * 64,
            user_agent_fingerprint="b" * 64,
        )


@pytest.mark.asyncio
async def test_exact_retry_is_durable_atomic_and_side_effect_free(
    engine: AsyncEngine,
) -> None:
    account, conversations = await _seed(engine)
    conversation = conversations[0]
    key = uuid.uuid4()

    first = await _create(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        key=key,
    )
    replay = await _create(
        engine,
        account=account,
        conversation_id=conversation.conversation_id,
        key=key,
    )

    assert first.replayed is False
    assert replay.replayed is True
    assert first.ticket.ticket_id == replay.ticket.ticket_id

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(EscalationTicket)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(OperatorAuditEvent)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(
                    OperatorEscalationIdempotency
                )
            )
            == 1
        )
        ticket = await session.scalar(select(EscalationTicket))
        audit = await session.scalar(select(OperatorAuditEvent))
        idempotency = await session.scalar(
            select(OperatorEscalationIdempotency)
        )
        persisted_conversation = await session.get(
            Conversation, conversation.conversation_id
        )
        message = await session.scalar(select(Message))
        security = await session.scalar(select(OperatorAuditSecurityMetadata))

        assert ticket is not None
        assert ticket.operator_reason == REASON
        assert ticket.escalation_type == "complex_issue"
        assert ticket.priority == "medium"
        assert ticket.source == "operator_browser"
        assert ticket.created_by_account_id == account.account_id
        assert ticket.transcript_snapshot == []
        assert ticket.assigned_to is None
        assert ticket.resolution_notes is None
        assert ticket.assigned_at is None
        assert ticket.resolved_at is None

        assert audit is not None
        assert audit.category == "business"
        assert audit.actor_kind == "human"
        assert audit.actor_account_id == account.account_id
        assert audit.actor_display_name == account.display_name
        assert audit.effective_role == "operator"
        assert audit.request_id == "e2-postgres-request"
        assert audit.action == "operator_escalation_created"
        assert audit.target_id == str(ticket.ticket_id)
        assert audit.reason_code == "complex_issue"
        assert audit.event_metadata["priority"] == "medium"
        assert REASON not in str(audit.event_metadata)

        assert idempotency is not None
        assert idempotency.state == "completed"
        assert idempotency.ticket_id == ticket.ticket_id
        assert idempotency.response_status_code == 201
        assert idempotency.reservation_token is None
        assert str(key) not in repr(idempotency.__dict__)
        assert REASON not in repr(idempotency.__dict__)
        assert audit.idempotency_reference == idempotency.key_digest

        assert security is not None
        assert security.source_network_fingerprint == "a" * 64
        assert security.user_agent_fingerprint == "b" * 64

        assert persisted_conversation is not None
        assert persisted_conversation.status == "qualifying"
        assert persisted_conversation.context == {
            "automation_state": "unchanged",
            "handoff": "bot",
        }
        assert persisted_conversation.message_count == 1
        assert persisted_conversation.updated_at == conversation.updated_at
        assert message is not None
        assert (
            message.content
            == "Authoritative conversation content must not be copied."
        )


@pytest.mark.asyncio
async def test_key_body_conflict_and_new_key_already_open(
    engine: AsyncEngine,
) -> None:
    account, conversations = await _seed(engine)
    conversation_id = conversations[0].conversation_id
    key = uuid.uuid4()
    await _create(
        engine,
        account=account,
        conversation_id=conversation_id,
        key=key,
    )

    with pytest.raises(IdempotencyConflict):
        await _create(
            engine,
            account=account,
            conversation_id=conversation_id,
            key=key,
            reason="A different valid reason binds to a different request body.",
        )
    with pytest.raises(EscalationAlreadyOpen):
        await _create(
            engine,
            account=account,
            conversation_id=conversation_id,
            key=uuid.uuid4(),
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(EscalationTicket)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(
                    OperatorEscalationIdempotency
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_live_reservation_returns_in_progress(
    engine: AsyncEngine,
) -> None:
    account, conversations = await _seed(engine)
    conversation_id = conversations[0].conversation_id
    key = uuid.uuid4()
    key_digest, request_fingerprint = service._digests(
        secret=IDEMPOTENCY_SECRET,
        idempotency_key=key,
        conversation_id=conversation_id,
        reason=REASON,
        escalation_type="complex_issue",
        priority="medium",
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        reservation = await service._reserve(
            session,
            actor_account_id=account.account_id,
            key_digest=key_digest,
            request_fingerprint=request_fingerprint,
        )
        assert reservation.replayed is False

    with pytest.raises(IdempotencyInProgress):
        await _create(
            engine,
            account=account,
            conversation_id=conversation_id,
            key=key,
        )


@pytest.mark.asyncio
async def test_concurrent_independent_transactions_create_exactly_one_active(
    engine: AsyncEngine,
) -> None:
    account, conversations = await _seed(engine)
    conversation_id = conversations[0].conversation_id

    async def attempt(key: uuid.UUID):
        try:
            result = await _create(
                engine,
                account=account,
                conversation_id=conversation_id,
                key=key,
            )
            return ("created", result.ticket.ticket_id)
        except EscalationAlreadyOpen:
            return ("already_open", None)

    results = await asyncio.gather(attempt(uuid.uuid4()), attempt(uuid.uuid4()))
    assert sorted(result[0] for result in results) == [
        "already_open",
        "created",
    ]

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        active_count = await session.scalar(
            select(func.count())
            .select_from(EscalationTicket)
            .where(EscalationTicket.status.in_(("open", "in_progress")))
        )
        assert active_count == 1
        assert (
            await session.scalar(
                select(func.count()).select_from(OperatorAuditEvent)
            )
            == 1
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(
                    OperatorEscalationIdempotency
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_failed_business_transaction_rolls_back_everything(
    engine: AsyncEngine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    account, conversations = await _seed(engine)

    async def fail_audit(*_args, **_kwargs):
        raise RuntimeError("forced audit failure")

    monkeypatch.setattr(service, "append_operator_audit_event", fail_audit)
    with pytest.raises(RuntimeError, match="forced audit failure"):
        await _create(
            engine,
            account=account,
            conversation_id=conversations[0].conversation_id,
            key=uuid.uuid4(),
        )

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        assert (
            await session.scalar(
                select(func.count()).select_from(EscalationTicket)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(OperatorAuditEvent)
            )
            == 0
        )
        assert (
            await session.scalar(
                select(func.count()).select_from(
                    OperatorEscalationIdempotency
                )
            )
            == 0
        )


@pytest.mark.asyncio
async def test_high_priority_is_persisted_and_audited(
    engine: AsyncEngine,
) -> None:
    account, conversations = await _seed(engine)
    result = await _create(
        engine,
        account=account,
        conversation_id=conversations[0].conversation_id,
        key=uuid.uuid4(),
        escalation_type="payment_issue",
        priority="high",
    )
    assert result.ticket.priority == "high"
    assert result.ticket.reason == "sav_issue"

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        audit = await session.scalar(select(OperatorAuditEvent))
        assert audit is not None
        assert audit.reason_code == "payment_issue"
        assert audit.event_metadata["priority"] == "high"
        assert audit.event_metadata["source"] == "operator_browser"


@pytest.mark.asyncio
async def test_stale_matching_reservation_can_be_safely_reclaimed(
    engine: AsyncEngine,
) -> None:
    account, conversations = await _seed(engine)
    conversation_id = conversations[0].conversation_id
    key = uuid.uuid4()
    key_digest, request_fingerprint = service._digests(
        secret=IDEMPOTENCY_SECRET,
        idempotency_key=key,
        conversation_id=conversation_id,
        reason=REASON,
        escalation_type="complex_issue",
        priority="medium",
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add(
            OperatorEscalationIdempotency(
                actor_account_id=account.account_id,
                key_digest=key_digest,
                request_fingerprint=request_fingerprint,
                state="in_progress",
                reservation_token=uuid.uuid4(),
                locked_until=datetime.now(timezone.utc) - timedelta(seconds=1),
            )
        )
        await session.commit()

    result = await _create(
        engine,
        account=account,
        conversation_id=conversation_id,
        key=key,
    )
    assert result.replayed is False


@pytest.mark.asyncio
async def test_legacy_create_ticket_interface_and_behavior_are_preserved(
    engine: AsyncEngine,
) -> None:
    _account, conversations = await _seed(engine)
    conversation = conversations[0]
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        result = await create_legacy_ticket(
            session,
            conversation_id=conversation.conversation_id,
            customer_id=conversation.customer_id,
            reason="voice_note",
            priority="high",
        )
        await session.commit()

    assert result == {
        "escalated": True,
        "escalation_id": result["escalation_id"],
        "reason": "voice_note",
        "priority": "high",
    }
    assert uuid.UUID(result["escalation_id"])

    async with factory() as session:
        ticket = await session.scalar(select(EscalationTicket))
        persisted_conversation = await session.get(
            Conversation, conversation.conversation_id
        )
        assert ticket is not None
        assert ticket.reason == "voice_note"
        assert ticket.source == "legacy"
        assert ticket.escalation_type is None
        assert ticket.operator_reason is None
        assert ticket.created_by_account_id is None
        assert len(ticket.transcript_snapshot) == 1
        assert persisted_conversation is not None
        assert persisted_conversation.status == "escalated"
