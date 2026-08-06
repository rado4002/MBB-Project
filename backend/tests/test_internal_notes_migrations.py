from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


REVISION = "e5f6a7b8c9d0"


def test_internal_notes_migration_is_additive_seed_free_and_reversible() -> None:
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    assert script.get_current_head() == REVISION
    assert script.get_revision(REVISION).down_revision == "d4e5f6a7b8c9"

    source = (
        Path("alembic/versions") / "e5f6a7b8c9d0_add_internal_notes.py"
    ).read_text(encoding="utf-8")
    lowered = source.lower()
    assert "internal_notes" in source
    assert "op.bulk_insert" not in lowered
    assert "insert into" not in lowered
    assert "messages" not in source
    assert "def downgrade()" in source
    assert "d4e5f6a7b8c9" in source


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("D1_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable D1 PostgreSQL database",
)
async def test_disposable_database_upgrade_downgrade_and_reupgrade() -> None:
    database_url = os.environ["D1_TEST_DATABASE_URL"]
    environment = {**os.environ, "DATABASE_URL": database_url}
    for arguments in (
        ("upgrade", "head"),
        ("downgrade", "d4e5f6a7b8c9"),
        ("upgrade", "head"),
    ):
        subprocess.run(
            [sys.executable, "-m", "alembic", *arguments],
            cwd=Path.cwd(),
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        )

    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            assert await connection.scalar(
                text(
                    "SELECT to_regclass('mbb.internal_notes') IS NOT NULL"
                )
            )
            assert await connection.scalar(
                text(
                    "SELECT version_num = 'e5f6a7b8c9d0' "
                    "FROM mbb.alembic_version"
                )
            )
    finally:
        await engine.dispose()
