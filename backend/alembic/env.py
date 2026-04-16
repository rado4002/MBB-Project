"""Alembic migration environment — async SQLAlchemy + mbb schema."""
import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# ── App imports ───────────────────────────────────────────────────────────────
# Import Base AND all models so autogenerate can detect them.
from app.database import Base
import app.models  # noqa: F401 — registers all ORM classes on Base.metadata
from app.config import get_settings

# ── Alembic Config ────────────────────────────────────────────────────────────
config = context.config

# Inject the database URL from settings (reads env vars / Docker secrets).
settings = get_settings()
config.set_main_option("sqlalchemy.url", settings.database_url)

# Interpret the config file for Python logging.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Restrict autogenerate to the mbb schema only.
target_metadata = Base.metadata


# ── Offline Mode ──────────────────────────────────────────────────────────────
def run_migrations_offline() -> None:
    """Run migrations without a live DB connection (outputs SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_schemas=True,
        version_table_schema="mbb",
    )
    with context.begin_transaction():
        context.run_migrations()


# ── Online Mode (async) ───────────────────────────────────────────────────────
def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        include_schemas=True,
        version_table_schema="mbb",
        compare_type=True,
    )
    with context.begin_transaction():
        # Ensure mbb schema exists before migrations run
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS mbb"))
        context.run_migrations()


async def run_async_migrations() -> None:
    """Run migrations using an async engine."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
