from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get("AI4E_MIGRATION_DATABASE_URL")
PREVIOUS_REVISION = "c9d0e1f2a3b4"
HEAD_REVISION = "d0e1f2a3b4c5"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI4E_MIGRATION_DATABASE_URL is required for disposable PostgreSQL evidence",
)


def _migrate(command: str, revision: str) -> None:
    assert DATABASE_URL is not None
    parsed = make_url(DATABASE_URL)
    environment = {
        **os.environ,
        "POSTGRES_HOST": str(parsed.host),
        "POSTGRES_PORT": str(parsed.port),
        "POSTGRES_DB": str(parsed.database),
        "POSTGRES_USER": str(parsed.username),
        "POSTGRES_PASSWORD": str(parsed.password),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_ai4e_reason_constraint_round_trip_preserves_historical_reason() -> None:
    assert DATABASE_URL is not None
    _migrate("downgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    conversation_id = uuid.uuid4()
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO mbb.customers "
                    "(phone_number, name, city, preferred_language) VALUES "
                    "('+243810004099', 'AI4E Migration', 'Kinshasa', 'french')"
                )
            )
            await connection.execute(
                text(
                    "INSERT INTO mbb.conversations "
                    "(conversation_id, customer_id, language_detected) VALUES "
                    "(:conversation_id, '+243810004099', 'french')"
                ),
                {"conversation_id": conversation_id},
            )
            await connection.execute(
                text(
                    "INSERT INTO mbb.escalation_tickets "
                    "(conversation_id, customer_id, reason, source, "
                    "escalation_type, status, transcript_snapshot) VALUES "
                    "(:conversation_id, '+243810004099', 'human_handoff', "
                    "'ai_capability', 'human_handoff', 'resolved', '[]'::jsonb)"
                ),
                {"conversation_id": conversation_id},
            )
        await engine.dispose()

        _migrate("upgrade", HEAD_REVISION)
        engine = create_async_engine(DATABASE_URL)
        reasons = (
            "qualified_purchase_intent",
            "explicit_human_request",
            "authority_required",
            "reliability_tool_failure",
        )
        async with engine.begin() as connection:
            for reason in reasons:
                await connection.execute(
                    text(
                        "INSERT INTO mbb.escalation_tickets "
                        "(conversation_id, customer_id, reason, source, "
                        "escalation_type, status, transcript_snapshot) VALUES "
                        "(:conversation_id, '+243810004099', :reason, "
                        "'ai_capability', 'human_handoff', 'resolved', '[]'::jsonb)"
                    ),
                    {"conversation_id": conversation_id, "reason": reason},
                )
            assert await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mbb.escalation_tickets "
                    "WHERE conversation_id = :conversation_id "
                    "AND reason = 'human_handoff'"
                ),
                {"conversation_id": conversation_id},
            ) == 1
        await engine.dispose()

        _migrate("downgrade", PREVIOUS_REVISION)
        engine = create_async_engine(DATABASE_URL)
        async with engine.connect() as connection:
            assert await connection.scalar(
                text(
                    "SELECT COUNT(*) FROM mbb.escalation_tickets "
                    "WHERE conversation_id = :conversation_id "
                    "AND reason = 'human_handoff'"
                ),
                {"conversation_id": conversation_id},
            ) == 5
        await engine.dispose()
        _migrate("upgrade", "head")
    finally:
        await engine.dispose()
