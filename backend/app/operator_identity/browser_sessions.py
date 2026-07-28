"""Dedicated fail-closed Redis DB 4 storage for future browser sessions."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import redis.asyncio as aioredis

from app.config import Settings, get_settings

SESSION_KEY_PREFIX = "mbb:browser_session:v1:"
ACCOUNT_INDEX_PREFIX = "mbb:browser_account_sessions:v1:"
SESSION_TOKEN_BYTES = 32


class SessionStoreUnavailable(RuntimeError):
    """Raised whenever session state cannot be checked authoritatively."""


class InvalidSessionToken(ValueError):
    """Raised for structurally invalid raw session tokens."""


@dataclass(frozen=True)
class BrowserSessionRecord:
    session_ref: str
    account_id: uuid.UUID
    auth_version: int
    created_at_epoch: int
    last_activity_at_epoch: int
    absolute_expires_at_epoch: int
    recent_reauthenticated_at_epoch: int
    csrf_generation: int
    ip_prefix_fingerprint: str | None
    user_agent_fingerprint: str | None

    @classmethod
    def from_redis_hash(cls, values: dict[str, str]) -> "BrowserSessionRecord":
        return cls(
            session_ref=values["session_ref"],
            account_id=uuid.UUID(values["account_id"]),
            auth_version=int(values["auth_version"]),
            created_at_epoch=int(values["created_at_epoch"]),
            last_activity_at_epoch=int(values["last_activity_at_epoch"]),
            absolute_expires_at_epoch=int(values["absolute_expires_at_epoch"]),
            recent_reauthenticated_at_epoch=int(
                values["recent_reauthenticated_at_epoch"]
            ),
            csrf_generation=int(values["csrf_generation"]),
            ip_prefix_fingerprint=values.get("ip_prefix_fingerprint") or None,
            user_agent_fingerprint=values.get("user_agent_fingerprint") or None,
        )


@dataclass(frozen=True)
class CreatedBrowserSession:
    token: str = field(repr=False)
    record: BrowserSessionRecord
    removed_session_refs: tuple[str, ...]


@dataclass(frozen=True)
class RotatedBrowserSession:
    token: str = field(repr=False)
    session_ref: str


@dataclass(frozen=True)
class ActivityUpdateResult:
    active: bool
    updated: bool


_CREATE_SESSION_SCRIPT = r"""
-- browser_session_create_v1
local now = tonumber(ARGV[1])
local idle = tonumber(ARGV[2])
local maximum = tonumber(ARGV[3])
local session_prefix = ARGV[4]
local new_ref = ARGV[5]
local removed = {}

local existing = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, ref in ipairs(existing) do
    local key = session_prefix .. ref
    if redis.call('EXISTS', key) == 0 then
        redis.call('ZREM', KEYS[1], ref)
        table.insert(removed, ref)
    else
        local absolute_expiry = tonumber(redis.call('HGET', key, 'absolute_expires_at_epoch') or '0')
        local last_activity = tonumber(redis.call('HGET', key, 'last_activity_at_epoch') or '0')
        if absolute_expiry <= now or last_activity + idle <= now then
            redis.call('DEL', key)
            redis.call('ZREM', KEYS[1], ref)
            table.insert(removed, ref)
        end
    end
end

redis.call(
    'HSET', KEYS[2],
    'session_ref', new_ref,
    'account_id', ARGV[6],
    'auth_version', ARGV[7],
    'created_at_epoch', ARGV[8],
    'last_activity_at_epoch', ARGV[9],
    'absolute_expires_at_epoch', ARGV[10],
    'recent_reauthenticated_at_epoch', ARGV[11],
    'csrf_generation', ARGV[12],
    'ip_prefix_fingerprint', ARGV[13],
    'user_agent_fingerprint', ARGV[14]
)
redis.call('EXPIREAT', KEYS[2], tonumber(ARGV[10]))
local redis_time = redis.call('TIME')
local creation_score = tonumber(ARGV[8]) + (tonumber(redis_time[2]) / 1000000)
redis.call('ZADD', KEYS[1], creation_score, new_ref)

local excess = redis.call('ZCARD', KEYS[1]) - maximum
if excess > 0 then
    local oldest = redis.call('ZRANGE', KEYS[1], 0, excess - 1)
    for _, ref in ipairs(oldest) do
        redis.call('DEL', session_prefix .. ref)
        redis.call('ZREM', KEYS[1], ref)
        table.insert(removed, ref)
    end
end

local latest_expiry = tonumber(ARGV[10])
local remaining = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, ref in ipairs(remaining) do
    local candidate = tonumber(redis.call('HGET', session_prefix .. ref, 'absolute_expires_at_epoch') or '0')
    if candidate > latest_expiry then
        latest_expiry = candidate
    end
end
redis.call('EXPIREAT', KEYS[1], latest_expiry)
return removed
"""

_GET_SESSION_SCRIPT = r"""
-- browser_session_get_v1
if redis.call('EXISTS', KEYS[1]) == 0 then
    return {}
end
local now = tonumber(ARGV[1])
local idle = tonumber(ARGV[2])
local absolute_expiry = tonumber(redis.call('HGET', KEYS[1], 'absolute_expires_at_epoch') or '0')
local last_activity = tonumber(redis.call('HGET', KEYS[1], 'last_activity_at_epoch') or '0')
if absolute_expiry <= now or last_activity + idle <= now then
    local account_id = redis.call('HGET', KEYS[1], 'account_id')
    redis.call('DEL', KEYS[1])
    if account_id then
        redis.call('ZREM', ARGV[3] .. account_id, ARGV[4])
    end
    return {}
end
return redis.call('HGETALL', KEYS[1])
"""

_UPDATE_ACTIVITY_SCRIPT = r"""
-- browser_session_activity_v1
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
local now = tonumber(ARGV[1])
local idle = tonumber(ARGV[2])
local coalesce = tonumber(ARGV[3])
local absolute_expiry = tonumber(redis.call('HGET', KEYS[1], 'absolute_expires_at_epoch') or '0')
local last_activity = tonumber(redis.call('HGET', KEYS[1], 'last_activity_at_epoch') or '0')
if absolute_expiry <= now or last_activity + idle <= now then
    local account_id = redis.call('HGET', KEYS[1], 'account_id')
    redis.call('DEL', KEYS[1])
    if account_id then
        redis.call('ZREM', ARGV[4] .. account_id, ARGV[5])
    end
    return 0
end
if now - last_activity < coalesce then
    return 1
end
redis.call('HSET', KEYS[1], 'last_activity_at_epoch', now)
return 2
"""

_ROTATE_SESSION_SCRIPT = r"""
-- browser_session_rotate_v1
if redis.call('EXISTS', KEYS[1]) == 0 or redis.call('EXISTS', KEYS[2]) == 1 then
    return 0
end
local now = tonumber(ARGV[1])
local idle = tonumber(ARGV[2])
local absolute_expiry = tonumber(redis.call('HGET', KEYS[1], 'absolute_expires_at_epoch') or '0')
local last_activity = tonumber(redis.call('HGET', KEYS[1], 'last_activity_at_epoch') or '0')
local account_id = redis.call('HGET', KEYS[1], 'account_id')
if absolute_expiry <= now or last_activity + idle <= now or not account_id then
    redis.call('DEL', KEYS[1])
    if account_id then
        redis.call('ZREM', ARGV[3] .. account_id, ARGV[4])
    end
    return 0
end
local csrf_generation = tonumber(redis.call('HGET', KEYS[1], 'csrf_generation') or '0') + 1
local values = redis.call('HGETALL', KEYS[1])
redis.call('HSET', KEYS[2], unpack(values))
redis.call('HSET', KEYS[2], 'session_ref', ARGV[5], 'csrf_generation', csrf_generation)
redis.call('EXPIREAT', KEYS[2], absolute_expiry)
local account_index = ARGV[3] .. account_id
local score = redis.call('ZSCORE', account_index, ARGV[4]) or redis.call('HGET', KEYS[1], 'created_at_epoch')
redis.call('DEL', KEYS[1])
redis.call('ZREM', account_index, ARGV[4])
redis.call('ZADD', account_index, score, ARGV[5])
return 1
"""

_REVOKE_ONE_SCRIPT = r"""
-- browser_session_revoke_one_v1
if redis.call('EXISTS', KEYS[1]) == 0 then
    return 0
end
local account_id = redis.call('HGET', KEYS[1], 'account_id')
redis.call('DEL', KEYS[1])
if account_id then
    redis.call('ZREM', ARGV[1] .. account_id, ARGV[2])
end
return 1
"""

_REVOKE_ALL_SCRIPT = r"""
-- browser_session_revoke_all_v1
local refs = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, ref in ipairs(refs) do
    redis.call('DEL', ARGV[1] .. ref)
end
redis.call('DEL', KEYS[1])
return refs
"""

_CLEAN_ACCOUNT_INDEX_SCRIPT = r"""
-- browser_session_cleanup_v1
local now = tonumber(ARGV[1])
local idle = tonumber(ARGV[2])
local removed = {}
local refs = redis.call('ZRANGE', KEYS[1], 0, -1)
for _, ref in ipairs(refs) do
    local key = ARGV[3] .. ref
    local absolute_expiry = tonumber(redis.call('HGET', key, 'absolute_expires_at_epoch') or '0')
    local last_activity = tonumber(redis.call('HGET', key, 'last_activity_at_epoch') or '0')
    if redis.call('EXISTS', key) == 0 or absolute_expiry <= now or last_activity + idle <= now then
        redis.call('DEL', key)
        redis.call('ZREM', KEYS[1], ref)
        table.insert(removed, ref)
    end
end
if redis.call('ZCARD', KEYS[1]) == 0 then
    redis.call('DEL', KEYS[1])
end
return removed
"""

_browser_session_pool: aioredis.ConnectionPool | None = None


def get_browser_session_pool(
    settings: Settings | None = None,
) -> aioredis.ConnectionPool:
    global _browser_session_pool
    configured = settings or get_settings()
    if _browser_session_pool is None:
        _browser_session_pool = aioredis.ConnectionPool.from_url(
            configured.browser_session_redis_url,
            max_connections=10,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=5,
            retry_on_timeout=False,
        )
    return _browser_session_pool


def get_browser_session_client(settings: Settings | None = None) -> aioredis.Redis:
    return aioredis.Redis(connection_pool=get_browser_session_pool(settings))


class BrowserSessionStore:
    """Authoritative session operations. Every Redis error fails closed."""

    def __init__(
        self,
        *,
        redis_client: Any | None = None,
        settings: Settings | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.settings = settings or get_settings()
        self._redis = redis_client or get_browser_session_client(self.settings)
        self._clock = clock

    def _secret(self) -> bytes:
        secret = self.settings.browser_session_hmac_secret
        if not secret or len(secret.encode("utf-8")) < 32:
            raise SessionStoreUnavailable(
                "browser session HMAC secret is missing or too short"
            )
        return secret.encode("utf-8")

    @staticmethod
    def generate_token() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(SESSION_TOKEN_BYTES)).rstrip(
            b"="
        ).decode("ascii")

    @staticmethod
    def _validate_token(token: str) -> None:
        if not isinstance(token, str) or len(token) != 43:
            raise InvalidSessionToken("invalid browser session token")
        try:
            decoded = base64.urlsafe_b64decode(token + "=")
        except (ValueError, TypeError) as exc:
            raise InvalidSessionToken("invalid browser session token") from exc
        if len(decoded) != SESSION_TOKEN_BYTES:
            raise InvalidSessionToken("invalid browser session token")

    def session_ref(self, raw_token: str) -> str:
        self._validate_token(raw_token)
        return hmac.new(
            self._secret(),
            f"session:v1:{raw_token}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def fingerprint(self, value: str | None, *, purpose: str) -> str | None:
        if value is None:
            return None
        return hmac.new(
            self._secret(),
            f"{purpose}:v1:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    @staticmethod
    def _session_key(session_ref: str) -> str:
        return f"{SESSION_KEY_PREFIX}{session_ref}"

    @staticmethod
    def _account_index_key(account_id: uuid.UUID | str) -> str:
        return f"{ACCOUNT_INDEX_PREFIX}{account_id}"

    def _now(self, explicit_now: int | None = None) -> int:
        return int(self._clock()) if explicit_now is None else int(explicit_now)

    async def _eval(
        self, script: str, keys: list[str], args: list[str | int]
    ) -> Any:
        self._secret()
        try:
            return await self._redis.eval(script, len(keys), *keys, *args)
        except SessionStoreUnavailable:
            raise
        except Exception as exc:  # Redis is an authorization dependency: fail closed.
            raise SessionStoreUnavailable("browser session store is unavailable") from exc

    async def create_session(
        self,
        *,
        account_id: uuid.UUID,
        auth_version: int,
        recent_reauthenticated_at_epoch: int | None = None,
        ip_prefix: str | None = None,
        user_agent: str | None = None,
        now_epoch: int | None = None,
    ) -> CreatedBrowserSession:
        if auth_version < 1:
            raise ValueError("auth_version must be positive")
        now = self._now(now_epoch)
        absolute_expiry = now + self.settings.browser_session_absolute_seconds
        raw_token = self.generate_token()
        session_ref = self.session_ref(raw_token)
        record = BrowserSessionRecord(
            session_ref=session_ref,
            account_id=account_id,
            auth_version=auth_version,
            created_at_epoch=now,
            last_activity_at_epoch=now,
            absolute_expires_at_epoch=absolute_expiry,
            recent_reauthenticated_at_epoch=(
                now
                if recent_reauthenticated_at_epoch is None
                else int(recent_reauthenticated_at_epoch)
            ),
            csrf_generation=1,
            ip_prefix_fingerprint=self.fingerprint(ip_prefix, purpose="network"),
            user_agent_fingerprint=self.fingerprint(user_agent, purpose="user-agent"),
        )
        removed = await self._eval(
            _CREATE_SESSION_SCRIPT,
            [
                self._account_index_key(account_id),
                self._session_key(session_ref),
            ],
            [
                now,
                self.settings.browser_session_idle_seconds,
                self.settings.browser_max_sessions_per_account,
                SESSION_KEY_PREFIX,
                session_ref,
                str(account_id),
                auth_version,
                now,
                now,
                absolute_expiry,
                record.recent_reauthenticated_at_epoch,
                record.csrf_generation,
                record.ip_prefix_fingerprint or "",
                record.user_agent_fingerprint or "",
            ],
        )
        return CreatedBrowserSession(
            token=raw_token,
            record=record,
            removed_session_refs=tuple(str(value) for value in removed),
        )

    async def get_session(
        self, raw_token: str, *, now_epoch: int | None = None
    ) -> BrowserSessionRecord | None:
        session_ref = self.session_ref(raw_token)
        values = await self._eval(
            _GET_SESSION_SCRIPT,
            [self._session_key(session_ref)],
            [
                self._now(now_epoch),
                self.settings.browser_session_idle_seconds,
                ACCOUNT_INDEX_PREFIX,
                session_ref,
            ],
        )
        if not values:
            return None
        try:
            mapping = dict(zip(values[::2], values[1::2], strict=True))
            return BrowserSessionRecord.from_redis_hash(mapping)
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionStoreUnavailable("browser session record is malformed") from exc

    async def update_activity(
        self, raw_token: str, *, now_epoch: int | None = None
    ) -> ActivityUpdateResult:
        session_ref = self.session_ref(raw_token)
        result = int(
            await self._eval(
                _UPDATE_ACTIVITY_SCRIPT,
                [self._session_key(session_ref)],
                [
                    self._now(now_epoch),
                    self.settings.browser_session_idle_seconds,
                    self.settings.browser_session_activity_coalesce_seconds,
                    ACCOUNT_INDEX_PREFIX,
                    session_ref,
                ],
            )
        )
        return ActivityUpdateResult(active=result > 0, updated=result == 2)

    async def rotate_session(
        self, raw_token: str, *, now_epoch: int | None = None
    ) -> RotatedBrowserSession | None:
        old_ref = self.session_ref(raw_token)
        new_token = self.generate_token()
        new_ref = self.session_ref(new_token)
        result = await self._eval(
            _ROTATE_SESSION_SCRIPT,
            [self._session_key(old_ref), self._session_key(new_ref)],
            [
                self._now(now_epoch),
                self.settings.browser_session_idle_seconds,
                ACCOUNT_INDEX_PREFIX,
                old_ref,
                new_ref,
            ],
        )
        if int(result) != 1:
            return None
        return RotatedBrowserSession(token=new_token, session_ref=new_ref)

    async def revoke_session(self, raw_token: str) -> bool:
        session_ref = self.session_ref(raw_token)
        result = await self._eval(
            _REVOKE_ONE_SCRIPT,
            [self._session_key(session_ref)],
            [ACCOUNT_INDEX_PREFIX, session_ref],
        )
        return bool(result)

    async def revoke_all_sessions(
        self, account_id: uuid.UUID
    ) -> tuple[str, ...]:
        removed = await self._eval(
            _REVOKE_ALL_SCRIPT,
            [self._account_index_key(account_id)],
            [SESSION_KEY_PREFIX],
        )
        return tuple(str(value) for value in removed)

    async def cleanup_account_sessions(
        self, account_id: uuid.UUID, *, now_epoch: int | None = None
    ) -> tuple[str, ...]:
        removed = await self._eval(
            _CLEAN_ACCOUNT_INDEX_SCRIPT,
            [self._account_index_key(account_id)],
            [
                self._now(now_epoch),
                self.settings.browser_session_idle_seconds,
                SESSION_KEY_PREFIX,
            ],
        )
        return tuple(str(value) for value in removed)
