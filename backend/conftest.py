"""
backend/conftest.py — Pytest configuration for all test modules.

On Windows, asyncpg is incompatible with the default ProactorEventLoop.
We force SelectorEventLoop and share one session-scoped loop so async
DB tests don't hit "Event loop is closed" / "NoneType.send" errors.
"""
import asyncio
import sys
import pytest

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
