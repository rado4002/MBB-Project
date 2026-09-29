"""Quantity cutover against an isolated migration database."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DATABASE_URL = os.environ.get("AI2B_MIGRATION_DATABASE_URL")
PREVIOUS_REVISION = "e7f8a9b0c1d2"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="AI2B_MIGRATION_DATABASE_URL is required"
)


def _migrate(command: str, revision: str) -> None:
    assert DATABASE_URL is not None
    from sqlalchemy.engine import make_url

    parsed = make_url(DATABASE_URL)
    environment = {
        **os.environ,
        "POSTGRES_HOST": str(parsed.host),
        "POSTGRES_PORT": str(parsed.port),
        "POSTGRES_DB": str(parsed.database),
        "POSTGRES_USER": str(parsed.username),
        "POSTGRES_PASSWORD": str(parsed.password or ""),
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", command, revision],
        env=environment, check=True, capture_output=True, text=True,
    )


@pytest.mark.asyncio
async def test_legacy_statuses_convert_without_inventing_stock() -> None:
    assert DATABASE_URL is not None
    _migrate("upgrade", "head")
    _migrate("downgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    ids = [uuid.uuid4() for _ in range(3)]
    product_id = uuid.uuid4()
    original_updates = {}
    async with engine.begin() as connection:
        await connection.execute(
            text("INSERT INTO mbb.products (product_id, name, category_code, description) "
                 "VALUES (:id, 'Fictional migration product', 'migration', 'Isolated test')"),
            {"id": product_id},
        )
        for item_id, status in zip(ids, ("available", "out_of_stock", "unknown")):
            await connection.execute(
                text("INSERT INTO mbb.sellable_items (sellable_item_id, product_id) "
                     "VALUES (:item, :product)"),
                {"item": item_id, "product": product_id},
            )
            await connection.execute(
                text("INSERT INTO mbb.inventory_statuses (sellable_item_id, status) "
                     "VALUES (:item, :status)"),
                {"item": item_id, "status": status},
            )
        original_updates = dict((await connection.execute(
            text("SELECT sellable_item_id, updated_at FROM mbb.inventory_statuses "
                 "WHERE sellable_item_id = ANY(:ids)"), {"ids": ids},
        )).all())
    await engine.dispose()

    _migrate("upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    async with engine.begin() as connection:
        rows = (await connection.execute(
            text("SELECT sellable_item_id, status, quantity, updated_at "
                 "FROM mbb.inventory_statuses WHERE sellable_item_id = ANY(:ids)"),
            {"ids": ids},
        )).all()
        by_id = {row.sellable_item_id: row for row in rows}
        assert len(by_id) == 3
        assert [(by_id[item_id].status, by_id[item_id].quantity) for item_id in ids] == [
            ("available", None), ("out_of_stock", 0), ("unknown", None)
        ]
        assert all(by_id[item_id].updated_at == original_updates[item_id] for item_id in ids)
        assert await connection.scalar(text("SELECT COUNT(*) FROM mbb.order_drafts")) == 0
    await engine.dispose()
