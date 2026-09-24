"""Isolated PostgreSQL evidence for the no-send Relance V2 candidate slice."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.escalation_ticket import EscalationTicket
from app.models.lead import Lead
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.relance import Relance
from app.models.relance_candidate import RelanceCandidate
from app.modules.m1_gateway.service import process_inbound
from app.modules.m6_relance.candidates import create_candidates
from app.tasks import relance as tasks


DATABASE_URL = os.environ.get("E2_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="E2_TEST_DATABASE_URL requires disposable PostgreSQL"
)


@pytest_asyncio.fixture(loop_scope="function")
async def factory():
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE mbb.customers RESTART IDENTITY CASCADE"))
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        async with engine.begin() as connection:
            await connection.execute(text("TRUNCATE mbb.customers RESTART IDENTITY CASCADE"))
        await engine.dispose()


async def _seed(
    factory, *, age_hours: float = 25, lead_exists: bool = True,
    owner: str = "ai", ai_state: str = "eligible", opt_out: bool = False,
    escalation: bool = False,
):
    now = datetime.now(timezone.utc)
    old = now - timedelta(hours=age_hours)
    phone = f"+243{uuid.uuid4().int % 10**9:09d}"
    conversation_id = uuid.uuid4()
    message_id = uuid.uuid4()
    lead_id = uuid.uuid4() if lead_exists else None
    async with factory() as session:
        if owner == "human":
            account_id = uuid.uuid4()
            session.add(OperatorAccount(
                account_id=account_id,
                username_normalized=f"operator.{account_id.hex[:12]}",
                display_name="Relance Test Operator",
                password_hash="not-used", role="operator", status="active",
                auth_version=1, must_change_password=False,
            ))
        else:
            account_id = None
        session.add(Customer(
            phone_number=phone, city="Kinshasa", preferred_language="french",
            opt_out_flag=opt_out,
        ))
        session.add(Conversation(
            conversation_id=conversation_id, customer_id=phone,
            language_detected="french", status="active", context={},
            owner_type=owner, human_owner_account_id=account_id,
            ai_execution_state=ai_state, last_message_time=old,
        ))
        session.add(Message(
            message_id=message_id, conversation_id=conversation_id,
            timestamp=old, created_at=old, direction="inbound",
            content="I am interested", content_type="text", language="french",
            whatsapp_message_id=f"relance-{message_id}",
        ))
        if lead_id is not None:
            session.add(Lead(
                lead_id=lead_id, customer_id=phone, conversation_id=conversation_id,
                score="warm", score_value=5, stage="consideration",
                intent="buy", product_interest=[], source="whatsapp",
            ))
        if escalation:
            session.add(EscalationTicket(
                conversation_id=conversation_id, customer_id=phone,
                reason="complex_complaint", priority="medium", status="open",
                transcript_snapshot=[],
            ))
        await session.commit()
    return phone, conversation_id, lead_id, message_id


async def _scan(factory, *, delay_hours: float = 24):
    async with factory() as session:
        result = await create_candidates(session, delay_hours=delay_hours)
        await session.commit()
        return result


async def _candidates(factory):
    async with factory() as session:
        return (await session.scalars(select(RelanceCandidate))).all()


@pytest.mark.asyncio
async def test_eligible_candidate_is_durable_and_repeated_scan_is_idempotent(factory, monkeypatch):
    _, _, lead_id, _ = await _seed(factory)
    monkeypatch.setattr(tasks, "AsyncSessionLocal", factory)
    send = Mock()
    monkeypatch.setattr(tasks.send_relance, "apply_async", send)
    first = await tasks._scan_and_schedule_relances()
    second = await tasks._scan_and_schedule_relances()
    rows = await _candidates(factory)
    assert first["candidate_count"] == 1
    assert second["candidate_count"] == 0
    assert len(rows) == 1 and rows[0].lead_id == lead_id
    assert rows[0].attempt_number == 1 and rows[0].confirmed_sent_at is None
    assert rows[0].cancelled_at is None
    send.assert_not_called()
    assert tasks.send_relance.run(str(uuid.uuid4()))["status"] == "obsolete"


@pytest.mark.asyncio
@pytest.mark.parametrize("case,kwargs", [
    ("recent", {"age_hours": 2}),
    ("human", {"owner": "human", "ai_state": "paused"}),
    ("ai_paused", {"ai_state": "paused"}),
    ("escalation", {"escalation": True}),
    ("opt_out", {"opt_out": True}),
    ("no_lead", {"lead_exists": False}),
])
async def test_ineligible_conversations_make_no_candidate(factory, case, kwargs):
    await _seed(factory, **kwargs)
    assert (await _scan(factory))[1] == 0, case
    assert await _candidates(factory) == []


@pytest.mark.asyncio
async def test_configurable_delay_and_kinshasa_quiet_hours(factory):
    await _seed(factory)
    assert (await _scan(factory, delay_hours=48))[1] == 0
    async with factory() as session:
        _, created = await create_candidates(
            session, delay_hours=24,
            now=datetime.now(timezone.utc).replace(
                hour=22, minute=0, second=0, microsecond=0
            ) + timedelta(days=1),
        )
        await session.commit()
    assert created == 1
    row = (await _candidates(factory))[0]
    assert row.scheduled_at.hour == 6  # 07:00 Africa/Kinshasa


@pytest.mark.asyncio
async def test_existing_active_candidate_and_concurrent_scans_do_not_duplicate(factory):
    await _seed(factory)
    assert (await _scan(factory))[1] == 1
    assert (await _scan(factory))[1] == 0
    assert len(await _candidates(factory)) == 1


@pytest.mark.asyncio
async def test_parallel_scans_serialize_on_customer_row(factory):
    await _seed(factory)
    results = await asyncio.gather(_scan(factory), _scan(factory))
    assert sorted(result[1] for result in results) == [0, 1]
    assert len(await _candidates(factory)) == 1


@pytest.mark.asyncio
async def test_inbound_cancels_candidate_in_authoritative_transaction(factory):
    phone, conversation_id, _, _ = await _seed(factory)
    assert (await _scan(factory))[1] == 1
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="Bonjour encore",
            content_type="text", timestamp=datetime.now(timezone.utc),
            whatsapp_message_id=f"relance-new-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        assert (await session.scalar(
            select(RelanceCandidate.cancelled_at).where(
                RelanceCandidate.lead_id.in_(
                    select(Lead.lead_id).where(Lead.conversation_id == conversation_id)
                )
            )
        )) is not None
        await session.commit()
    assert (await _scan(factory))[1] == 0
    assert len(await _candidates(factory)) == 1


@pytest.mark.asyncio
async def test_cancelled_episode_is_not_recreated_but_new_episode_can_qualify(factory):
    phone, _, _, _ = await _seed(factory)
    assert (await _scan(factory))[1] == 1
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="I am back",
            content_type="text", timestamp=datetime.now(timezone.utc),
            whatsapp_message_id=f"relance-new-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        await session.commit()
    assert (await _scan(factory))[1] == 0
    async with factory() as session:
        eligible, created = await create_candidates(
            session, delay_hours=24,
            now=datetime.now(timezone.utc) + timedelta(hours=25),
        )
        await session.commit()
    assert (eligible, created) == (1, 1)
    rows = await _candidates(factory)
    assert len(rows) == 2
    assert sum(row.cancelled_at is None for row in rows) == 1
    assert all(row.attempt_number == 1 for row in rows)


@pytest.mark.asyncio
async def test_opt_out_inbound_cancels_candidates_across_customer_leads(factory):
    phone, _, _, _ = await _seed(factory)
    old = datetime.now(timezone.utc) - timedelta(hours=26)
    second_conversation_id = uuid.uuid4()
    async with factory() as session:
        session.add(Conversation(
            conversation_id=second_conversation_id, customer_id=phone,
            language_detected="french", status="active", context={},
            last_message_time=old,
        ))
        session.add(Message(
            message_id=uuid.uuid4(), conversation_id=second_conversation_id,
            timestamp=old, created_at=old, direction="inbound",
            content="A second conversation", content_type="text", language="french",
        ))
        session.add(Lead(
            lead_id=uuid.uuid4(), customer_id=phone,
            conversation_id=second_conversation_id, score="warm", score_value=5,
            stage="consideration", intent="buy", product_interest=[], source="whatsapp",
        ))
        await session.commit()
    assert (await _scan(factory))[1] == 2
    async with factory() as session:
        await process_inbound(
            session=session, customer_phone=phone, content="stop",
            content_type="text", timestamp=datetime.now(timezone.utc),
            whatsapp_message_id=f"relance-stop-{uuid.uuid4()}", message_id=uuid.uuid4(),
        )
        await session.commit()
    assert all(row.cancelled_at is not None for row in await _candidates(factory))


@pytest.mark.asyncio
async def test_outbound_does_not_reset_inbound_silence(factory):
    _, conversation_id, _, _ = await _seed(factory)
    now = datetime.now(timezone.utc)
    async with factory() as session:
        session.add(Message(
            message_id=uuid.uuid4(), conversation_id=conversation_id,
            timestamp=now, direction="outbound", content="Human or AI reply",
            content_type="text", language="french",
        ))
        await session.execute(
            update(Conversation).where(Conversation.conversation_id == conversation_id)
            .values(last_message_time=now)
        )
        await session.commit()
    assert (await _scan(factory))[1] == 1


@pytest.mark.asyncio
async def test_two_confirmed_sends_stop_candidate_creation(factory):
    _, _, lead_id, source_id = await _seed(factory)
    assert lead_id is not None
    now = datetime.now(timezone.utc)
    async with factory() as session:
        for attempt in (1, 2):
            session.add(RelanceCandidate(
                lead_id=lead_id, source_message_id=source_id,
                attempt_number=attempt, scheduled_at=now,
                confirmed_sent_at=now,
            ))
        await session.commit()
    assert (await _scan(factory))[1] == 0
    assert len(await _candidates(factory)) == 2


@pytest.mark.asyncio
async def test_legacy_scheduled_and_cancelled_candidates_do_not_count_as_sends(factory):
    _, _, lead_id, source_id = await _seed(factory)
    assert lead_id is not None
    now = datetime.now(timezone.utc)
    async with factory() as session:
        lead = await session.get(Lead, lead_id)
        lead.relance_count = 2
        session.add(Relance(
            lead_id=lead_id, attempt_number=1, scheduled_at=now,
            value_hook="Legacy scheduled text", hook_type="reciprocity",
        ))
        session.add(RelanceCandidate(
            lead_id=lead_id, source_message_id=source_id,
            attempt_number=1, scheduled_at=now,
            cancelled_at=now,
        ))
        await session.commit()
    # The cancelled row cannot be recreated for the same inbound episode.
    assert (await _scan(factory))[1] == 0
    async with factory() as session:
        session.add(Message(
            message_id=uuid.uuid4(), conversation_id=lead.conversation_id,
            timestamp=now - timedelta(hours=25),
            created_at=now - timedelta(hours=25), direction="inbound",
            content="Another episode", content_type="text", language="french",
        ))
        await session.commit()
    assert (await _scan(factory))[1] == 1
    rows = await _candidates(factory)
    assert len(rows) == 2
    assert sum(row.cancelled_at is None for row in rows) == 1
    assert all(row.attempt_number == 1 for row in rows)
