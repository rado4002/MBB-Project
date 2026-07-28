from __future__ import annotations

import asyncio
import inspect
import re
import time
import uuid

import fakeredis.aioredis
import pytest
import pytest_asyncio

from app.config import Settings
from app.operator_identity import browser_sessions
from app.operator_identity.browser_sessions import (
    ACCOUNT_INDEX_PREFIX,
    SESSION_KEY_PREFIX,
    BrowserSessionStore,
    SessionStoreUnavailable,
)


def _settings(**overrides) -> Settings:
    values = {
        "browser_session_hmac_secret": "s" * 32,
        "browser_session_redis_db": 4,
        "browser_session_idle_seconds": 1800,
        "browser_session_absolute_seconds": 28800,
        "browser_max_sessions_per_account": 2,
        "browser_session_activity_coalesce_seconds": 60,
    }
    values.update(overrides)
    return Settings(**values)


@pytest_asyncio.fixture(loop_scope="function")
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture(loop_scope="function")
async def store(redis_client):
    return BrowserSessionStore(redis_client=redis_client, settings=_settings())


def test_token_entropy_format_and_repr_safety() -> None:
    tokens = {BrowserSessionStore.generate_token() for _ in range(128)}
    assert len(tokens) == 128
    assert all(re.fullmatch(r"[A-Za-z0-9_-]{43}", token) for token in tokens)


@pytest.mark.asyncio
async def test_create_read_revoke_and_raw_token_not_stored(store, redis_client) -> None:
    account_id = uuid.uuid4()
    created = await store.create_session(
        account_id=account_id,
        auth_version=3,
        ip_prefix="192.0.2.0/24",
        user_agent="private full browser string",
    )
    assert created.token not in repr(created)
    record = await store.get_session(created.token)
    assert record == created.record
    assert record.auth_version == 3
    assert record.ip_prefix_fingerprint != "192.0.2.0/24"
    assert record.user_agent_fingerprint != "private full browser string"

    keys = await redis_client.keys("*")
    stored = []
    for key in keys:
        key_type = await redis_client.type(key)
        if key_type == "hash":
            stored.extend((await redis_client.hgetall(key)).values())
        elif key_type == "zset":
            stored.extend(await redis_client.zrange(key, 0, -1))
    assert created.token not in keys
    assert created.token not in stored
    assert await store.revoke_session(created.token) is True
    assert await store.get_session(created.token) is None


@pytest.mark.asyncio
async def test_rotation_replaces_token_and_increments_csrf_generation(store) -> None:
    created = await store.create_session(account_id=uuid.uuid4(), auth_version=1)
    rotated = await store.rotate_session(created.token)
    assert rotated is not None
    assert rotated.token not in repr(rotated)
    assert await store.get_session(created.token) is None
    replacement = await store.get_session(rotated.token)
    assert replacement is not None
    assert replacement.csrf_generation == 2
    assert replacement.account_id == created.record.account_id
    rotated_again = await store.rotate_session(rotated.token)
    assert rotated_again is not None
    second_replacement = await store.get_session(rotated_again.token)
    assert second_replacement is not None
    assert second_replacement.csrf_generation == 3


@pytest.mark.asyncio
async def test_rotation_can_establish_recent_reauthentication(store) -> None:
    now = int(time.time())
    created = await store.create_session(
        account_id=uuid.uuid4(),
        auth_version=1,
        recent_reauthenticated_at_epoch=now - 1000,
        now_epoch=now,
    )
    rotated = await store.rotate_session(
        created.token,
        now_epoch=now + 1,
        recent_reauthenticated_at_epoch=now + 1,
    )
    assert rotated is not None
    replacement = await store.get_session(rotated.token, now_epoch=now + 1)
    assert replacement is not None
    assert replacement.recent_reauthenticated_at_epoch == now + 1
    assert replacement.absolute_expires_at_epoch == created.record.absolute_expires_at_epoch


@pytest.mark.asyncio
async def test_activity_is_coalesced_and_idle_expiry_is_fail_closed(store) -> None:
    now = int(time.time())
    created = await store.create_session(
        account_id=uuid.uuid4(), auth_version=1, now_epoch=now
    )
    coalesced = await store.update_activity(created.token, now_epoch=now + 30)
    assert coalesced.active is True
    assert coalesced.updated is False
    updated = await store.update_activity(created.token, now_epoch=now + 61)
    assert updated.active is True
    assert updated.updated is True
    record = await store.get_session(created.token, now_epoch=now + 61)
    assert record is not None
    assert record.last_activity_at_epoch == now + 61
    assert (
        await store.get_session(created.token, now_epoch=now + 61 + 1801) is None
    )


@pytest.mark.asyncio
async def test_absolute_expiry_is_enforced(redis_client) -> None:
    store = BrowserSessionStore(redis_client=redis_client, settings=_settings())
    now = int(time.time())
    created = await store.create_session(
        account_id=uuid.uuid4(), auth_version=1, now_epoch=now
    )
    await redis_client.hset(
        f"{SESSION_KEY_PREFIX}{created.record.session_ref}",
        "last_activity_at_epoch",
        now + 28_799,
    )
    assert await store.get_session(created.token, now_epoch=now + 28799)
    assert await store.get_session(created.token, now_epoch=now + 28800) is None


@pytest.mark.asyncio
async def test_two_session_maximum_evicts_oldest(store, redis_client) -> None:
    account_id = uuid.uuid4()
    now = int(time.time())
    first = await store.create_session(
        account_id=account_id, auth_version=1, now_epoch=now
    )
    second = await store.create_session(
        account_id=account_id, auth_version=1, now_epoch=now + 1
    )
    third = await store.create_session(
        account_id=account_id, auth_version=1, now_epoch=now + 2
    )
    assert first.record.session_ref in third.removed_session_refs
    assert await store.get_session(first.token, now_epoch=now + 2) is None
    assert await store.get_session(second.token, now_epoch=now + 2) is not None
    assert await store.get_session(third.token, now_epoch=now + 2) is not None
    assert (
        await redis_client.zcard(f"{ACCOUNT_INDEX_PREFIX}{account_id}") == 2
    )


@pytest.mark.asyncio
async def test_concurrent_creation_never_leaves_three_sessions(store, redis_client) -> None:
    account_id = uuid.uuid4()
    now = int(time.time())
    created = await asyncio.gather(
        *(
            store.create_session(
                account_id=account_id, auth_version=1, now_epoch=now
            )
            for _ in range(8)
        )
    )
    index_key = f"{ACCOUNT_INDEX_PREFIX}{account_id}"
    assert await redis_client.zcard(index_key) == 2
    active = await asyncio.gather(
        *(store.get_session(item.token, now_epoch=now) for item in created)
    )
    assert sum(record is not None for record in active) == 2


@pytest.mark.asyncio
async def test_account_revocation_and_dangling_cleanup(store, redis_client) -> None:
    account_id = uuid.uuid4()
    created = await store.create_session(account_id=account_id, auth_version=1)
    dangling = "d" * 64
    index_key = f"{ACCOUNT_INDEX_PREFIX}{account_id}"
    await redis_client.zadd(index_key, {dangling: 0})
    removed = await store.cleanup_account_sessions(account_id)
    assert dangling in removed
    revoked = await store.revoke_all_sessions(account_id)
    assert created.record.session_ref in revoked
    assert await redis_client.exists(index_key) == 0
    assert await redis_client.exists(f"{SESSION_KEY_PREFIX}{created.record.session_ref}") == 0


@pytest.mark.asyncio
async def test_redis_unavailable_and_missing_secret_fail_closed() -> None:
    class UnavailableRedis:
        async def eval(self, *_args, **_kwargs):
            raise ConnectionError("unavailable")

    store = BrowserSessionStore(
        redis_client=UnavailableRedis(), settings=_settings()
    )
    with pytest.raises(SessionStoreUnavailable):
        await store.create_session(account_id=uuid.uuid4(), auth_version=1)

    missing_secret = BrowserSessionStore(
        redis_client=UnavailableRedis(),
        settings=_settings(browser_session_hmac_secret=""),
    )
    with pytest.raises(SessionStoreUnavailable):
        await missing_secret.create_session(account_id=uuid.uuid4(), auth_version=1)


def test_session_store_has_no_jwt_fallback() -> None:
    source = inspect.getsource(browser_sessions).lower()
    assert "jwt" not in source
