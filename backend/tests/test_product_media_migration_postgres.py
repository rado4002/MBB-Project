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

DATABASE_URL = os.environ.get("AI2C1_MIGRATION_DATABASE_URL")
PREVIOUS_REVISION = "b8c9d0e1f2a3"
MEDIA_REVISION = "c9d0e1f2a3b4"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2C1_MIGRATION_DATABASE_URL is required for disposable PostgreSQL evidence",
)


def _migration_environment(database_url: str) -> dict[str, str]:
    parsed = make_url(database_url)
    return {
        **os.environ,
        "POSTGRES_HOST": str(parsed.host),
        "POSTGRES_PORT": str(parsed.port),
        "POSTGRES_DB": str(parsed.database),
        "POSTGRES_USER": str(parsed.username),
        "POSTGRES_PASSWORD": str(parsed.password or ""),
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
async def test_product_media_migration_round_trip_preserves_existing_rows() -> None:
    assert DATABASE_URL is not None
    _migrate("upgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    ids = {key: uuid.uuid4() for key in ("account", "product", "item", "price", "inventory", "rate")}
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO mbb.customers (phone_number, name, city, preferred_language) "
                "VALUES ('+243810000091', 'Existing Media Customer', 'Kinshasa', 'french')"
            )
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.operator_accounts (account_id, username_normalized, "
                "display_name, password_hash, role, status, auth_version, "
                "must_change_password, password_changed_at, created_at, updated_at) "
                "VALUES (:id, 'media.existing', 'Existing Administrator', 'not-used', "
                "'administrator', 'active', 1, false, NOW(), NOW(), NOW())"
            ),
            {"id": ids["account"]},
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.products (product_id, name, category_code, description) "
                "VALUES (:id, 'Existing Fictional Product', 'air_fryer', 'Existing row')"
            ),
            {"id": ids["product"]},
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.sellable_items (sellable_item_id, product_id, model_label, sku) "
                "VALUES (:id, :product_id, '6L', 'EXISTING-MEDIA-6L')"
            ),
            {"id": ids["item"], "product_id": ids["product"]},
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.sellable_item_prices "
                "(price_id, sellable_item_id, amount, currency) "
                "VALUES (:id, :item_id, 55.00, 'USD')"
            ),
            {"id": ids["price"], "item_id": ids["item"]},
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.inventory_statuses (inventory_id, sellable_item_id, status) "
                "VALUES (:id, :item_id, 'available')"
            ),
            {"id": ids["inventory"], "item_id": ids["item"]},
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.exchange_rates "
                "(exchange_rate_id, base_currency, quote_currency, rate) "
                "VALUES (:id, 'USD', 'CDF', 2800.000000)"
            ),
            {"id": ids["rate"]},
        )
    await engine.dispose()

    _migrate("upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    media_id = uuid.uuid4()
    async with engine.begin() as connection:
        assert await connection.scalar(
            text("SELECT version_num = :revision FROM mbb.alembic_version"),
            {"revision": MEDIA_REVISION},
        )
        await connection.execute(
            text(
                "INSERT INTO mbb.product_media "
                "(media_id, product_id, asset_url, is_primary, display_order, active) "
                "VALUES (:media_id, :product_id, "
                "'https://example.invalid/product/migration.jpg', true, 0, true)"
            ),
            {"media_id": media_id, "product_id": ids["product"]},
        )
        assert await connection.scalar(
            text("SELECT COUNT(*) FROM mbb.sellable_items WHERE sellable_item_id = :id"),
            {"id": ids["item"]},
        ) == 1
    await engine.dispose()

    _migrate("downgrade", PREVIOUS_REVISION)
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT version_num = :revision FROM mbb.alembic_version"),
            {"revision": PREVIOUS_REVISION},
        )
        assert await connection.scalar(
            text("SELECT to_regclass('mbb.product_media') IS NULL")
        )
        for table, key, value in (
            ("customers", "phone_number", "+243810000091"),
            ("operator_accounts", "account_id", ids["account"]),
            ("products", "product_id", ids["product"]),
            ("sellable_items", "sellable_item_id", ids["item"]),
            ("sellable_item_prices", "price_id", ids["price"]),
            ("inventory_statuses", "inventory_id", ids["inventory"]),
            ("exchange_rates", "exchange_rate_id", ids["rate"]),
        ):
            assert await connection.scalar(
                text(f"SELECT COUNT(*) FROM mbb.{table} WHERE {key} = :value"),
                {"value": value},
            ) == 1
    await engine.dispose()

    _migrate("upgrade", "head")
    engine = create_async_engine(DATABASE_URL)
    async with engine.connect() as connection:
        assert await connection.scalar(
            text("SELECT version_num = :revision FROM mbb.alembic_version"),
            {"revision": MEDIA_REVISION},
        )
        assert await connection.scalar(
            text("SELECT to_regclass('mbb.product_media') IS NOT NULL")
        )
    await engine.dispose()
