from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

EXPECTED_HEAD = "c3d4e5f6a7b8"


def test_operator_migrations_extend_the_linear_chain_without_data_inserts() -> None:
    config = Config("alembic.ini")
    script = ScriptDirectory.from_config(config)
    assert script.get_current_head() == EXPECTED_HEAD
    ownership_revision = script.get_revision(EXPECTED_HEAD)
    assert ownership_revision.down_revision == "b2e2c3d4e5f6"
    b2_revision = script.get_revision("b2e2c3d4e5f6")
    assert b2_revision.down_revision == "b1e2c3d4e5f6"
    b1_revision = script.get_revision("b1e2c3d4e5f6")
    assert b1_revision.down_revision == "a4b5c6d7e8f9"
    audit_revision = script.get_revision("a4b5c6d7e8f9")
    assert audit_revision.down_revision == "f3a4b5c6d7e8"
    account_revision = script.get_revision("f3a4b5c6d7e8")
    assert account_revision.down_revision == "e2f3a4b5c6d7"

    versions = Path("alembic/versions")
    for migration_name in (
        "f3a4b5c6d7e8_add_operator_accounts.py",
        "a4b5c6d7e8f9_add_operator_audit.py",
    ):
        source = (versions / migration_name).read_text(encoding="utf-8").lower()
        assert "op.bulk_insert" not in source
        assert "insert into" not in source
        assert "downgrade" in source


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("D1_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable D1 PostgreSQL database",
)
async def test_upgraded_disposable_database_metadata_and_no_seeded_accounts() -> None:
    engine = create_async_engine(os.environ["D1_TEST_DATABASE_URL"])
    async with engine.connect() as connection:
        tables = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT table_name
                        FROM information_schema.tables
                        WHERE table_schema = 'mbb'
                        """
                    )
                )
            ).scalars()
        )
        assert {
            "operator_accounts",
            "operator_audit_events",
            "operator_audit_security_metadata",
            "admin_audit_log",
        } <= tables
        assert (
            await connection.scalar(
                text("SELECT COUNT(*) FROM mbb.operator_accounts")
            )
            == 0
        )

        constraints = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT constraint_name
                        FROM information_schema.table_constraints
                        WHERE table_schema = 'mbb'
                          AND table_name IN (
                              'operator_accounts',
                              'operator_audit_events',
                              'operator_audit_security_metadata'
                          )
                        """
                    )
                )
            ).scalars()
        )
        assert {
            "pk_operator_accounts",
            "uq_operator_accounts_username_normalized",
            "chk_operator_accounts_username_format",
            "chk_operator_accounts_auth_version_positive",
            "pk_operator_audit_events",
            "fk_operator_audit_events_actor_account_id",
            "pk_operator_audit_security_metadata",
            "fk_operator_audit_security_metadata_event_id",
        } <= constraints

        indexes = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT indexname
                        FROM pg_indexes
                        WHERE schemaname = 'mbb'
                        """
                    )
                )
            ).scalars()
        )
        assert {
            "uq_operator_accounts_email_normalized",
            "idx_operator_audit_events_retain_until",
            "idx_operator_audit_security_metadata_retain_until",
        } <= indexes

        legacy_columns = set(
            (
                await connection.execute(
                    text(
                        """
                        SELECT column_name
                        FROM information_schema.columns
                        WHERE table_schema = 'mbb'
                          AND table_name = 'admin_audit_log'
                        """
                    )
                )
            ).scalars()
        )
        assert legacy_columns == {
            "audit_id",
            "user_name",
            "user_role",
            "action",
            "target_entity",
            "target_id",
            "old_value",
            "new_value",
            "justification",
            "ip_address",
            "created_at",
        }
    await engine.dispose()
