"""
backend/conftest.py — Pytest configuration for all test modules.

On Windows, asyncpg is incompatible with the default ProactorEventLoop.
We force SelectorEventLoop while pytest-asyncio owns event-loop creation
and applies the loop scopes configured by pytest.ini and test markers.
"""
import asyncio
import sys

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
