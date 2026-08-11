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

DATABASE_URL = os.environ.get("AI2B_MIGRATION_DATABASE_URL")
PREVIOUS_REVISION = "a7b8c9d0e1f2"
AI2B_REVISION = "b8c9d0e1f2a3"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2B_MIGRATION_DATABASE_URL is required for disposable PostgreSQL evidence",
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


def _migrate(command: str, revision: str) -> None:
    assert DATABASE_URL is not None
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        cwd=Path.cwd(),
        env=_migration_environment(DATABASE_URL),
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.asyncio
async def test_ai2b_migration_round_trip_preserves_existing_business_rows() -> None:
    assert DATABASE_URL is not None
    _migrate("upgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    account_id = uuid.uuid4()
    async with engine.begin() as connection:
        await connection.execute(
            text(
                """
                INSERT INTO mbb.customers (
                    phone_number, name, city, preferred_language
                ) VALUES (
                    '+243810000081', 'Existing Customer', 'Kinshasa', 'french'
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO mbb.operator_accounts (
                    account_id, username_normalized, display_name, password_hash,
                    role, status, auth_version, must_change_password,
                    password_changed_at, created_at, updated_at
                ) VALUES (
                    :account_id, 'existing.admin', 'Existing Administrator',
                    'not-used', 'administrator', 'active', 1, false,
                    NOW(), NOW(), NOW()
                )
                """
            ),
            {"account_id": account_id},
        )
    await engine.dispose()

    _migrate("upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.customers WHERE phone_number = '+243810000081'")
        ) == 1
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.operator_accounts WHERE account_id = :id"),
            {"id": account_id},
        ) == 1
        assert await connection.scalar(
            text(
                "SELECT COUNT(*) = 5 FROM information_schema.tables "
                "WHERE table_schema = 'mbb' AND table_name IN "
                "('products', 'sellable_items', 'sellable_item_prices', "
                "'exchange_rates', 'inventory_statuses')"
            )
        )
    await engine.dispose()

    _migrate("downgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT version_num = :revision FROM mbb.alembic_version"),
            {"revision": PREVIOUS_REVISION},
        )
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.customers WHERE phone_number = '+243810000081'")
        ) == 1
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.operator_accounts WHERE account_id = :id"),
            {"id": account_id},
        ) == 1
    await engine.dispose()

    _migrate("upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT version_num = :revision FROM mbb.alembic_version"),
            {"revision": AI2B_REVISION},
        )
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.customers WHERE phone_number = '+243810000081'")
        ) == 1
    await engine.dispose()
