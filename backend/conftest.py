"""
backend/conftest.py — Pytest configuration for all test modules.

On Windows, asyncpg is incompatible with the default ProactorEventLoop.
We force SelectorEventLoop and share one session-scoped loop so async
DB tests don't hit "Event loop is closed" / "NoneType.send" errors.
"""
import asyncio
import sys
import pytest

# Standalone diagnostic scripts in tests/ that are NOT pytest test modules.
# They use sys.exit() at module level (crashes pytest collector) and define
# no test_ functions. Exclude them from pytest collection entirely.
collect_ignore = [
    "tests/test_blackout_simulation.py",
    "tests/test_migration.py",
    "tests/test_resilience.py",
    "tests/test_schema_api_validation.py",
    "tests/test_project_setup.py",
    "tests/test_phase1a.py",
]

# Windows: asyncpg requires SelectorEventLoop (not the default ProactorEventLoop)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest.fixture(scope="session")
def event_loop():
    """Session-scoped event loop: shared by all async tests in the session."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    yield loop
    loop.close()
