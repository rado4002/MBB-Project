from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get("AI1E_MIGRATION_DATABASE_URL")
PREVIOUS_REVISION = "f6a7b8c9d0e1"
AI1E_REVISION = "a7b8c9d0e1f2"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason=(
        "AI1E_MIGRATION_DATABASE_URL is required for disposable PostgreSQL evidence"
    ),
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


async def _invalid_update(engine, statement: str, parameters: dict) -> None:
    with pytest.raises(IntegrityError):
        async with engine.begin() as connection:
            await connection.execute(text(statement), parameters)


@pytest.mark.asyncio
async def test_ai_handoff_migration_round_trip_and_constraints() -> None:
    assert DATABASE_URL is not None
    _migrate(DATABASE_URL, "upgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)

    operator_id = uuid.uuid4()
    ai_conversation_id = uuid.uuid4()
    human_conversation_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO mbb.operator_accounts (
                    account_id, username_normalized, display_name, password_hash,
                    role, status, auth_version, must_change_password,
                    password_changed_at, created_at, updated_at
                ) VALUES (
                    :account_id, 'migration.operator', 'Migration Operator',
                    'not-used', 'operator', 'active', 1, false, NOW(), NOW(), NOW()
                )
                """
            ),
            {"account_id": operator_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO mbb.customers (
                    phone_number, name, city, preferred_language
                ) VALUES
                    ('+243810000071', 'Existing AI', 'Kinshasa', 'french'),
                    ('+243810000072', 'Existing Human', 'Kinshasa', 'french')
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO mbb.conversations (
                    conversation_id, customer_id, language_detected,
                    owner_type, human_owner_account_id, ai_execution_state,
                    ownership_version
                ) VALUES
                    (:ai_id, '+243810000071', 'french', 'ai', NULL, 'eligible', 1),
                    (:human_id, '+243810000072', 'french', 'human', :operator_id,
                     'paused', 2)
                """
            ),
            {
                "ai_id": ai_conversation_id,
                "human_id": human_conversation_id,
                "operator_id": operator_id,
            },
        )
        await connection.execute(
            text(
                """
                INSERT INTO mbb.escalation_tickets (
                    conversation_id, customer_id, priority, reason, source,
                    escalation_type, status, transcript_snapshot
                ) VALUES (
                    :conversation_id, '+243810000071', 'medium',
                    'complex_complaint', 'legacy', NULL, 'resolved', '[]'::jsonb
                )
                """
            ),
            {"conversation_id": ai_conversation_id},
        )
    await engine.dispose()

    _migrate(DATABASE_URL, "upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    waiting_conversation_id = uuid.uuid4()
    async with engine.begin() as connection:
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.conversations")
        ) == 2
        assert await connection.scalar(
            text(
                "SELECT COUNT(*) FROM mbb.escalation_tickets "
                "WHERE reason = 'complex_complaint' AND source = 'legacy'"
            )
        ) == 1
        await connection.execute(
            text(
                """
                INSERT INTO mbb.customers (
                    phone_number, name, city, preferred_language
                ) VALUES ('+243810000073', 'Waiting AI', 'Kinshasa', 'french')
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO mbb.conversations (
                    conversation_id, customer_id, language_detected,
                    owner_type, human_owner_account_id, ai_execution_state,
                    ownership_version
                ) VALUES (
                    :conversation_id, '+243810000073', 'french',
                    'ai', NULL, 'paused', 3
                )
                """
            ),
            {"conversation_id": waiting_conversation_id},
        )
        await connection.execute(
            text(
                """
                INSERT INTO mbb.escalation_tickets (
                    conversation_id, customer_id, priority, reason, source,
                    escalation_type, operator_reason, created_by_account_id,
                    status, transcript_snapshot
                ) VALUES (
                    :conversation_id, '+243810000073', 'medium',
                    'human_handoff', 'ai_capability', 'human_handoff', NULL, NULL,
                    'open', '[]'::jsonb
                )
                """
            ),
            {"conversation_id": waiting_conversation_id},
        )

    await _invalid_update(
        engine,
        "UPDATE mbb.conversations SET owner_type = 'human', "
        "human_owner_account_id = :operator_id, ai_execution_state = 'eligible' "
        "WHERE conversation_id = :conversation_id",
        {"operator_id": operator_id, "conversation_id": waiting_conversation_id},
    )
    await _invalid_update(
        engine,
        "UPDATE mbb.conversations SET owner_type = 'human', "
        "human_owner_account_id = NULL, ai_execution_state = 'paused' "
        "WHERE conversation_id = :conversation_id",
        {"conversation_id": waiting_conversation_id},
    )
    await _invalid_update(
        engine,
        "UPDATE mbb.conversations SET owner_type = 'ai', "
        "human_owner_account_id = :operator_id, ai_execution_state = 'paused' "
        "WHERE conversation_id = :conversation_id",
        {"operator_id": operator_id, "conversation_id": waiting_conversation_id},
    )
    await _invalid_update(
        engine,
        "UPDATE mbb.conversations SET ai_execution_state = 'unsupported' "
        "WHERE conversation_id = :conversation_id",
        {"conversation_id": waiting_conversation_id},
    )

    async with engine.begin() as connection:
        assert await connection.scalar(
            text(
                "SELECT COUNT(*) FROM mbb.escalation_tickets "
                "WHERE source = 'ai_capability' AND escalation_type = 'human_handoff' "
                "AND reason = 'human_handoff'"
            )
        ) == 1
        await connection.execute(
            text(
                "DELETE FROM mbb.escalation_tickets "
                "WHERE conversation_id = :conversation_id"
            ),
            {"conversation_id": waiting_conversation_id},
        )
        await connection.execute(
            text(
                "UPDATE mbb.conversations SET ai_execution_state = 'eligible' "
                "WHERE conversation_id = :conversation_id"
            ),
            {"conversation_id": waiting_conversation_id},
        )
    await engine.dispose()

    _migrate(DATABASE_URL, "downgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.conversations")
        ) == 3
        assert await connection.scalar(
            text(
                f"SELECT version_num = '{PREVIOUS_REVISION}' "
                "FROM mbb.alembic_version"
            )
        )
    await engine.dispose()

    _migrate(DATABASE_URL, "upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text(
                f"SELECT version_num = '{AI1E_REVISION}' "
                "FROM mbb.alembic_version"
            )
        )
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.conversations")
        ) == 3
    await engine.dispose()
