from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.ai.commercial_state import (
    CommercialStateRevisionConflict,
    CommercialStateUpdate,
    read_commercial_state,
    update_commercial_state,
)
from app.models.conversation import Conversation
from app.models.customer import Customer

DATABASE_URL = os.environ.get("E2_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="E2_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


@pytest_asyncio.fixture(loop_scope="function")
async def engine() -> AsyncEngine:
    assert DATABASE_URL is not None
    database_engine = create_async_engine(DATABASE_URL, pool_size=5)
    truncate = text(
        "TRUNCATE TABLE mbb.conversations, mbb.customers RESTART IDENTITY CASCADE"
    )
    async with database_engine.begin() as connection:
        await connection.execute(truncate)
    try:
        yield database_engine
    finally:
        async with database_engine.begin() as connection:
            await connection.execute(truncate)
        await database_engine.dispose()


async def _seed(engine: AsyncEngine) -> uuid.UUID:
    now = datetime(2026, 8, 10, 10, tzinfo=timezone.utc)
    customer = Customer(
        phone_number="+243810000091",
        name="Commercial State Customer",
        city="Kinshasa",
        preferred_language="french",
    )
    conversation = Conversation(
        conversation_id=uuid.uuid4(),
        customer_id=customer.phone_number,
        language_detected="french",
        context={
            "existing_key": {"preserve": True},
            "qualification_state": {"step": "q2_location"},
        },
        owner_type="ai",
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
        session.add_all([customer, conversation])
        await session.commit()
    return conversation.conversation_id


@pytest.mark.asyncio
async def test_jsonb_persistence_context_preservation_and_revision_conflict(
    engine: AsyncEngine,
) -> None:
    conversation_id = await _seed(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async with factory() as session:
        first = await update_commercial_state(
            session,
            conversation_id=conversation_id,
            expected_revision=0,
            state_update=CommercialStateUpdate(
                current_goal="find a portable option",
                expressed_needs=["easy to carry"],
            ),
        )
        await session.commit()
    assert first is not None and first.revision == 1

    async with factory() as session:
        stored = await read_commercial_state(session, conversation_id)
        context = await session.scalar(
            select(Conversation.context).where(
                Conversation.conversation_id == conversation_id
            )
        )
    assert stored == first
    assert context["existing_key"] == {"preserve": True}
    assert context["qualification_state"] == {"step": "q2_location"}

    async with factory() as winning_session:
        winner = await update_commercial_state(
            winning_session,
            conversation_id=conversation_id,
            expected_revision=1,
            state_update=CommercialStateUpdate(
                decision_constraints=[{"kind": "budget", "value": "maximum $35"}]
            ),
        )
        await winning_session.commit()
    assert winner is not None and winner.revision == 2

    async with factory() as stale_session:
        with pytest.raises(CommercialStateRevisionConflict) as captured:
            await update_commercial_state(
                stale_session,
                conversation_id=conversation_id,
                expected_revision=1,
                state_update=CommercialStateUpdate(current_goal="stale overwrite"),
            )
        await stale_session.rollback()
    assert captured.value.current_revision == 2

    async with factory() as session:
        final = await read_commercial_state(session, conversation_id)
        context = await session.scalar(
            select(Conversation.context).where(
                Conversation.conversation_id == conversation_id
            )
        )
    assert final == winner
    assert final.current_goal == "find a portable option"
    assert context["existing_key"] == {"preserve": True}
