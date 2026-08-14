from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.ai.audit import (
    AIProvenanceReferenceError,
    AITurnAuditRecord,
    AITurnOutcome,
    CapabilityAuditDecision,
    CapabilityAuditOutcome,
    CapabilityAuditSummary,
    CommercialStateField,
    append_ai_turn_audit,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.turn import AITurn
from app.models.ai_turn_audit import AITurnAudit
from app.models.conversation import Conversation
from app.models.customer import Customer
from app.models.message import Message

DATABASE_URL = os.environ.get("AI1D_TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI1D_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)


def _migration_environment(database_url: str) -> dict[str, str]:
    parsed = make_url(database_url)
    return {
        **os.environ,
        "POSTGRES_HOST": str(parsed.host),
        "POSTGRES_PORT": str(parsed.port),
        "POSTGRES_DB": str(parsed.database),
        "POSTGRES_USER": str(parsed.username),
        "POSTGRES_PASSWORD": str(parsed.password),
    }


def _migrate(database_url: str, command: str, revision: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=Path.cwd(),
        env=_migration_environment(database_url),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_migration_round_trip_references_and_existing_rows_are_preserved() -> None:
    assert DATABASE_URL is not None
    _migrate(DATABASE_URL, "upgrade", "e5f6a7b8c9d0")
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    conversation_id = uuid.uuid4()
    source_message_id = uuid.uuid4()
    outbound_message_id = uuid.uuid4()
    async with factory() as session:
        session.add(
            Customer(
                phone_number="+243810000092",
                name="AI audit migration fixture",
                city="Kinshasa",
                preferred_language="french",
            )
        )
        session.add(
            Conversation(
                conversation_id=conversation_id,
                customer_id="+243810000092",
                language_detected="french",
            )
        )
        session.add_all(
            [
                Message(
                    message_id=source_message_id,
                    conversation_id=conversation_id,
                    direction="inbound",
                    content="authoritative inbound content",
                    content_type="text",
                    language="french",
                ),
                Message(
                    message_id=outbound_message_id,
                    conversation_id=conversation_id,
                    direction="outbound",
                    content="authoritative outbound content",
                    content_type="text",
                    language="french",
                ),
            ]
        )
        await session.commit()
    await engine.dispose()

    _migrate(DATABASE_URL, "upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    turn_id = AITurn(
        user_content="test",
        language="french",
        expected_ownership_version=1,
        conversation_id=uuid.uuid4(),
    ).turn_id
    record = AITurnAuditRecord(
        turn_id=turn_id,
        conversation_id=conversation_id,
        source_message_id=source_message_id,
        outbound_message_id=outbound_message_id,
        policy_version=AI_SYSTEM_POLICY_VERSION,
        provider="deepseek",
        model="deepseek-chat",
        exposed_capabilities=("search_products",),
        capability_activity=(
            CapabilityAuditSummary(
                capability_name="search_products",
                decision=CapabilityAuditDecision.executed,
                outcome=CapabilityAuditOutcome.success,
            ),
        ),
        commercial_state_revision_before=4,
        commercial_state_revision_after=5,
        commercial_state_changed_fields=(CommercialStateField.purchase_intent,),
        outcome=AITurnOutcome.response_generated,
    )

    async with factory() as session:
        audit = await append_ai_turn_audit(session, record)
        await session.commit()
        stored = await session.get(AITurnAudit, turn_id)
        assert stored is audit
        assert stored.actor_id == "mbb_ai"
        assert stored.capability_activity == [
            {
                "capability_name": "search_products",
                "decision": "executed",
                "outcome": "success",
                "safe_code": None,
            }
        ]
        assert stored.commercial_state_changed_fields == ["purchase_intent"]
        assert not hasattr(stored, "content")
        assert await session.scalar(select(func.count()).select_from(Customer)) == 1
        assert await session.scalar(select(func.count()).select_from(Conversation)) == 1
        assert await session.scalar(select(func.count()).select_from(Message)) == 2

    missing_message = AITurnAuditRecord(
        turn_id=AITurn(
            user_content="test",
            language="french",
            expected_ownership_version=1,
            conversation_id=uuid.uuid4(),
        ).turn_id,
        conversation_id=conversation_id,
        source_message_id=uuid.uuid4(),
        policy_version=AI_SYSTEM_POLICY_VERSION,
        outcome=AITurnOutcome.failed,
    )
    async with factory() as session:
        with pytest.raises(AIProvenanceReferenceError):
            await append_ai_turn_audit(session, missing_message)
        await session.rollback()

    async with engine.connect() as connection:
        constraints = set(
            (
                await connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = 'mbb' AND table_name = 'ai_turn_audits'"
                    )
                )
            ).scalars()
        )
        assert {
            "pk_ai_turn_audits",
            "fk_ai_turn_audits_conversation_id",
            "fk_ai_turn_audits_source_message_id",
            "fk_ai_turn_audits_outbound_message_id",
            "chk_ai_turn_audits_actor_id",
        } <= constraints
    await engine.dispose()

    _migrate(DATABASE_URL, "downgrade", "e5f6a7b8c9d0")
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert not await connection.scalar(
            text("SELECT to_regclass('mbb.ai_turn_audits') IS NOT NULL")
        )
        assert await connection.scalar(text("SELECT COUNT(*) FROM mbb.customers")) == 1
        assert await connection.scalar(text("SELECT COUNT(*) FROM mbb.conversations")) == 1
        assert await connection.scalar(text("SELECT COUNT(*) FROM mbb.messages")) == 2
    await engine.dispose()

    _migrate(DATABASE_URL, "upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT to_regclass('mbb.ai_turn_audits') IS NOT NULL")
        )
        assert await connection.scalar(
            text(
                "SELECT version_num = 'a7b8c9d0e1f2' FROM mbb.alembic_version"
            )
        )
        assert await connection.scalar(text("SELECT COUNT(*) FROM mbb.customers")) == 1
    await engine.dispose()
