"""Slice 6: lifecycle/tickets cannot manufacture execution authority."""
from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.deps import get_current_role
from app.api.v1.conversations import router
from app.database import get_db
from app.models.conversation import Conversation
from app.models.escalation_ticket import EscalationTicket
from app.models.message import Message
from app.modules.m1_gateway.service import process_inbound
from app.modules.m4_conversation.ai_handoff import request_human_handoff
from app.modules.m4_conversation.ownership import ai_may_reply
from app.modules.m8_maps.escalation import assign_ticket, create_ticket, resolve_ticket
from app.schemas.common import ConversationStatus
from test_conversation_ownership_postgres import _seed, _transition, engine  # noqa: F401
from test_operator_escalation_postgres import _create

pytestmark = pytest.mark.skipif(
    not os.environ.get("E2_TEST_DATABASE_URL"),
    reason="E2_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


def _api(factory):
    api = FastAPI()
    api.include_router(router, prefix="/api/v1")

    async def db():
        async with factory() as session:
            yield session
            await session.commit()

    api.dependency_overrides[get_db] = db
    api.dependency_overrides[get_current_role] = lambda: "admin"
    return api


async def _inbound(factory, phone, content_type="text"):
    async with factory() as session:
        inbound = await process_inbound(
            session=session, customer_phone=phone, content="Bonjour",
            content_type=content_type, timestamp=datetime.now(timezone.utc),
            whatsapp_message_id=str(uuid.uuid4()), message_id=uuid.uuid4(),
        )
        await session.commit()
        return inbound


def _authority(conversation):
    return (
        conversation.owner_type, conversation.human_owner_account_id,
        conversation.ai_execution_state, conversation.ownership_version,
        conversation.ownership_updated_at,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("lifecycle", list(ConversationStatus))
@pytest.mark.parametrize("authority", ["ai", "paused", "human"])
async def test_http_lifecycle_and_m1_selection_cannot_change_authority(
    engine, lifecycle, authority,
):
    accounts, conversation, _ = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if authority == "human":
        await _transition(
            engine, account=accounts[0], conversation_id=conversation.conversation_id,
            target="human", expected_version=1, key=uuid.uuid4(),
        )
    elif authority == "paused":
        async with factory() as session:
            await request_human_handoff(
                session, conversation_id=conversation.conversation_id,
                expected_ownership_version=1,
            )

    async with factory() as session:
        before = _authority(await session.get(Conversation, conversation.conversation_id))
        # A historical active AI conversation must not win over the latest
        # paused/human conversation just because of its lifecycle label.
        session.add(Conversation(
            conversation_id=uuid.uuid4(), customer_id=conversation.customer_id,
            status="active", language_detected="french",
            last_message_time=conversation.last_message_time - timedelta(days=1),
        ))
        await session.commit()

    api = _api(factory)
    url = f"/api/v1/conversations/{conversation.conversation_id}"
    async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
        response = await client.put(url + "/status", params={"new_status": lifecycle.value})
        assert response.status_code == 200
        assert response.json()["status"] == lifecycle.value
        assert (await client.put(url + "/status", params={"new_status": "bot"})).status_code == 422
        assert (await client.put(url + "/handoff", json={"mode": "bot"})).status_code == 404

    # Two independent transactions serialize selection through the customer
    # upsert. Neither may create a replacement or select the older AI record.
    arrivals = await asyncio.gather(
        _inbound(factory, conversation.customer_id),
        _inbound(factory, conversation.customer_id, "voice_note"),
    )
    assert all(item.conversation_id == conversation.conversation_id for item in arrivals)
    assert arrivals[1].is_voice_note and arrivals[1].requires_escalation
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        assert _authority(stored) == before
        assert stored.status == lifecycle.value
        assert stored.message_count == conversation.message_count + 2
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 2
        assert await ai_may_reply(
            session, conversation.conversation_id,
            expected_ownership_version=before[3],
        ) is (authority == "ai")
        assert not await ai_may_reply(
            session, conversation.conversation_id,
            expected_ownership_version=before[3] - 1,
        )


@pytest.mark.asyncio
async def test_first_concurrent_inbounds_create_one_conversation(engine):
    factory = async_sessionmaker(engine, expire_on_commit=False)
    arrivals = await asyncio.gather(
        _inbound(factory, "+243810000099"),
        _inbound(factory, "+243810000099"),
    )
    assert arrivals[0].conversation_id == arrivals[1].conversation_id
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        assert (await session.get(Conversation, arrivals[0].conversation_id)).message_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["ai", "paused", "human"])
async def test_ticket_creation_assignment_resolution_preserve_authority_and_history(engine, authority):
    accounts, conversation, _ = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    if authority == "human":
        await _transition(
            engine, account=accounts[0], conversation_id=conversation.conversation_id,
            target="human", expected_version=1, key=uuid.uuid4(),
        )
    async with factory() as session:
        if authority == "paused":
            result = await request_human_handoff(
                session, conversation_id=conversation.conversation_id,
                expected_ownership_version=1,
            )
            ticket_id = result.escalation_ticket_id
        else:
            result = await create_ticket(
                session, conversation_id=conversation.conversation_id,
                customer_id=conversation.customer_id, reason="voice_note", priority="high",
            )
            ticket_id = uuid.UUID(result["escalation_id"])
            await session.commit()
        before = _authority(await session.get(Conversation, conversation.conversation_id))
        await assign_ticket(session, ticket_id, "Ticket reviewer")
        await session.commit()
        assert _authority(await session.get(Conversation, conversation.conversation_id)) == before
        await resolve_ticket(session, ticket_id, "Reviewed; ownership unchanged", "Ticket reviewer")
        await session.commit()
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        assert _authority(stored) == before
        assert stored.status == "qualifying"
        assert await ai_may_reply(session, conversation.conversation_id) is (authority == "ai")
        historical = await session.get(EscalationTicket, ticket_id)
        assert historical.status == "resolved"
        assert historical.resolution_notes == "Reviewed; ownership unchanged"
        assert historical.resolved_at is not None
        if authority == "paused":
            assert stored.human_owner_account_id is None
        with pytest.raises(ValueError, match="already resolved"):
            await assign_ticket(session, ticket_id, "Another reviewer")
        await session.rollback()


@pytest.mark.asyncio
async def test_concurrent_operator_escalation_and_takeover_are_independent(engine):
    accounts, conversation, _ = await _seed(engine)
    ticket, takeover = await asyncio.gather(
        _create(engine, account=accounts[0], conversation_id=conversation.conversation_id, key=uuid.uuid4()),
        _transition(engine, account=accounts[1], conversation_id=conversation.conversation_id,
                    target="human", expected_version=1, key=uuid.uuid4()),
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        assert stored.human_owner_account_id == accounts[1].account_id
        assert stored.ownership_version == takeover.ownership.version == 2
        assert stored.ai_execution_state == "paused" and stored.status == "qualifying"
        persisted = await session.get(EscalationTicket, ticket.ticket.ticket_id)
        assert persisted.created_by_account_id == accounts[0].account_id
        assert persisted.status == "open"
        assert await session.scalar(select(func.count()).select_from(EscalationTicket)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["human", "ai", "second_resolution"])
async def test_concurrent_resolution_preserves_explicit_ownership_transition(engine, transition):
    accounts, conversation, _ = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        handoff = await request_human_handoff(
            session, conversation_id=conversation.conversation_id, expected_ownership_version=1,
        )
    if transition == "ai":
        await _transition(
            engine, account=accounts[0], conversation_id=conversation.conversation_id,
            target="human", expected_version=2, key=uuid.uuid4(),
        )

    async def resolve():
        async with factory() as session:
            # Deliberately preload the row to prove the lock refreshes an ORM
            # identity map after another transaction changes ticket status.
            preloaded = await session.get(EscalationTicket, handoff.escalation_ticket_id)
            assert preloaded is not None
            try:
                result = await resolve_ticket(session, handoff.escalation_ticket_id, "Reviewed", "Reviewer")
                await session.commit()
                return result
            except ValueError as exc:
                await session.rollback()
                return exc

    other = resolve() if transition == "second_resolution" else _transition(
        engine, account=accounts[0], conversation_id=conversation.conversation_id,
        target=transition, expected_version=3 if transition == "ai" else 2, key=uuid.uuid4(),
    )
    results = await asyncio.gather(resolve(), other)
    if transition == "second_resolution":
        assert sum(isinstance(result, ValueError) for result in results) == 1
    elif isinstance(results[0], ValueError):
        assert transition == "ai" and "already closed" in str(results[0])
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        expected_owner = "human" if transition == "human" else "ai"
        assert stored.owner_type == expected_owner
        assert stored.ownership_version == {"human": 3, "ai": 4, "second_resolution": 2}[transition]
        assert stored.ai_execution_state == ("eligible" if transition == "ai" else "paused")
        assert stored.status == "qualifying"
        assert await ai_may_reply(session, conversation.conversation_id) is (transition == "ai")
        assert not await ai_may_reply(session, conversation.conversation_id, expected_ownership_version=1)
        historical = await session.get(EscalationTicket, handoff.escalation_ticket_id)
        assert historical.status in ("resolved", "closed")
        assert await session.scalar(select(func.count()).select_from(EscalationTicket)) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["assign", "resolve"])
async def test_stale_ticket_reader_cannot_overwrite_committed_resolution(engine, operation):
    _, conversation, _ = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        created = await create_ticket(
            session, conversation_id=conversation.conversation_id,
            customer_id=conversation.customer_id, reason="voice_note", priority="high",
        )
        ticket_id = uuid.UUID(created["escalation_id"])
        await session.commit()
    async with factory() as stale_session:
        stale = await stale_session.get(EscalationTicket, ticket_id)
        assert stale.status == "open"
        async with factory() as winner:
            await resolve_ticket(winner, ticket_id, "First resolution", "First reviewer")
            await winner.commit()
        assert stale.status == "open"
        with pytest.raises(ValueError, match="already resolved"):
            if operation == "assign":
                await assign_ticket(stale_session, ticket_id, "Late reviewer")
            else:
                await resolve_ticket(stale_session, ticket_id, "Overwrite", "Late reviewer")
        await stale_session.rollback()
    async with factory() as session:
        stored = await session.get(EscalationTicket, ticket_id)
        assert stored.status == "resolved"
        assert stored.resolution_notes == "First resolution"
        assert stored.assigned_to == "First reviewer"


@pytest.mark.asyncio
@pytest.mark.parametrize("authority", ["ai", "paused", "human"])
async def test_m1_voice_note_routing_and_replay_respect_authority(engine, monkeypatch, authority):
    from types import SimpleNamespace

    import app.adapters as adapters
    import app.database as database
    from app.i18n.messages import t
    from app.tasks import m1

    accounts, conversation, _ = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(m1.settings, "whatsapp_send_enabled", False)
    monkeypatch.setattr(m1.settings, "m1_maps_fanout_enabled", False)

    def forbidden(*args, **kwargs):
        pytest.fail("voice-note authority path attempted external/provider execution")

    for name in ("get_messaging_adapter", "get_crm_adapter", "get_payment_adapter", "get_ai_adapter"):
        monkeypatch.setattr(adapters, name, forbidden)
    monkeypatch.setattr(m1, "_dispatch_maps_fanout", forbidden)

    if authority == "human":
        await _transition(
            engine, account=accounts[0], conversation_id=conversation.conversation_id,
            target="human", expected_version=1, key=uuid.uuid4(),
        )
    elif authority == "paused":
        async with factory() as session:
            await request_human_handoff(
                session, conversation_id=conversation.conversation_id,
                expected_ownership_version=1,
            )
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        stored.status = "escalated"
        before = _authority(stored)
        await session.commit()

    task = SimpleNamespace(retry=forbidden, request=SimpleNamespace(retries=0))
    args = dict(
        task=task, message_id=str(uuid.uuid4()), customer_phone=conversation.customer_id,
        content="[note vocale]", content_type="voice_note",
        timestamp=datetime.now(timezone.utc).isoformat(), whatsapp_message_id=str(uuid.uuid4()),
    )
    first = await m1._process(**args)
    assert first["conversation_id"] == str(conversation.conversation_id)
    assert first["status"] == {
        "ai": "escalated_voice_note", "paused": "waiting_for_human", "human": "human_controlled",
    }[authority]
    assert first["send_status"] == "skipped"
    duplicate = await m1._process(**args)
    assert duplicate["status"] == first["send_status"] == "skipped"
    assert duplicate["conversation_id"] == first["conversation_id"]
    async with factory() as session:
        stored = await session.get(Conversation, conversation.conversation_id)
        assert _authority(stored) == before and stored.status == "escalated"
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        outbound = list((await session.scalars(select(Message).where(Message.direction == "outbound"))).all())
        tickets = list((await session.scalars(select(EscalationTicket))).all())
        if authority == "ai":
            assert len(outbound) == len(tickets) == 1
            assert outbound[0].content == t("voice_note_ack", "french")
            assert tickets[0].reason == "voice_note" and tickets[0].priority == "high"
        else:
            assert outbound == []
            assert len(tickets) == (1 if authority == "paused" else 0)
