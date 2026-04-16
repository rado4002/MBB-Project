from typing import AsyncGenerator

import redis.asyncio as aioredis

from app.config import get_settings

settings = get_settings()

_pool: aioredis.ConnectionPool | None = None


def get_redis_pool() -> aioredis.ConnectionPool:
    global _pool
    if _pool is None:
        _pool = aioredis.ConnectionPool.from_url(
            settings.redis_url,
            max_connections=20,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=True,
        )
    return _pool


async def get_redis() -> AsyncGenerator[aioredis.Redis, None]:
    """FastAPI dependency — yields a Redis client from the shared pool."""
    client = aioredis.Redis(connection_pool=get_redis_pool())
    try:
        yield client
    finally:
        await client.aclose()
