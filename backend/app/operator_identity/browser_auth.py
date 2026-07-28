"""Fail-closed pre-authentication, CSRF, fingerprint and throttle primitives."""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from app.config import Settings, get_settings
from app.operator_identity.browser_sessions import (
    BrowserSessionRecord,
    BrowserSessionStore,
    InvalidSessionToken,
    SessionStoreUnavailable,
)

PREAUTH_COOKIE_NAME = "__Host-mbb_preauth"
SESSION_COOKIE_NAME = "__Host-mbb_session"
PREAUTH_KEY_PREFIX = "mbb:browser_preauth:v1:"
RATE_KEY_PREFIX = "mbb:browser_auth_failures:v1:"
OPAQUE_TOKEN_BYTES = 32


@dataclass(frozen=True)
class PreAuthContext:
    context_ref: str
    created_at_epoch: int
    expires_at_epoch: int
    csrf_generation: int
    source_network_fingerprint: str | None
    user_agent_fingerprint: str | None


@dataclass(frozen=True)
class CreatedPreAuthContext:
    token: str = field(repr=False)
    context: PreAuthContext


class BrowserAuthState:
    """Browser auth state in dedicated Redis DB 4; every failure denies access."""

    def __init__(
        self,
        *,
        redis_client: Any,
        settings: Settings | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._redis = redis_client
        self.settings = settings or get_settings()
        self._clock = clock
        self.sessions = BrowserSessionStore(
            redis_client=redis_client,
            settings=self.settings,
            clock=clock,
        )

    def _secret(self, value: str, label: str) -> bytes:
        encoded = value.encode("utf-8")
        if len(encoded) < 32:
            raise SessionStoreUnavailable(f"{label} is missing or too short")
        return encoded

    def _session_secret(self) -> bytes:
        return self._secret(
            self.settings.browser_session_hmac_secret,
            "browser session HMAC secret",
        )

    def _csrf_secret(self) -> bytes:
        return self._secret(
            self.settings.browser_csrf_hmac_secret,
            "browser CSRF HMAC secret",
        )

    @staticmethod
    def generate_token() -> str:
        return base64.urlsafe_b64encode(secrets.token_bytes(OPAQUE_TOKEN_BYTES)).rstrip(
            b"="
        ).decode("ascii")

    @staticmethod
    def _validate_token(token: str) -> None:
        if not isinstance(token, str) or len(token) != 43:
            raise InvalidSessionToken("invalid opaque browser token")
        try:
            decoded = base64.urlsafe_b64decode(token + "=")
        except (TypeError, ValueError) as exc:
            raise InvalidSessionToken("invalid opaque browser token") from exc
        if len(decoded) != OPAQUE_TOKEN_BYTES:
            raise InvalidSessionToken("invalid opaque browser token")

    def _digest(self, purpose: str, value: str) -> str:
        return hmac.new(
            self._session_secret(),
            f"{purpose}:v1:{value}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def fingerprint(self, value: str | None, *, purpose: str) -> str | None:
        if value is None:
            return None
        return self._digest(purpose, value)

    def source_network(self, host: str | None) -> str | None:
        if not host:
            return None
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return None
        prefix = 24 if address.version == 4 else 56
        return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))

    def csrf_for_preauth(self, context: PreAuthContext) -> str:
        digest = hmac.new(
            self._csrf_secret(),
            (
                f"preauth-csrf:v1:{context.context_ref}:"
                f"{context.csrf_generation}"
            ).encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    def csrf_for_session(self, record: BrowserSessionRecord) -> str:
        digest = hmac.new(
            self._csrf_secret(),
            f"session-csrf:v1:{record.session_ref}:{record.csrf_generation}".encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    async def _call(self, method: str, *args: Any, **kwargs: Any) -> Any:
        self._session_secret()
        self._csrf_secret()
        try:
            return await getattr(self._redis, method)(*args, **kwargs)
        except SessionStoreUnavailable:
            raise
        except Exception as exc:
            raise SessionStoreUnavailable(
                "browser authentication state is unavailable"
            ) from exc

    async def create_preauth(
        self,
        *,
        source_network: str | None,
        user_agent: str | None,
        now_epoch: int | None = None,
    ) -> CreatedPreAuthContext:
        now = int(self._clock()) if now_epoch is None else int(now_epoch)
        token = self.generate_token()
        context_ref = self._digest("preauth", token)
        context = PreAuthContext(
            context_ref=context_ref,
            created_at_epoch=now,
            expires_at_epoch=now + self.settings.browser_preauth_seconds,
            csrf_generation=1,
            source_network_fingerprint=self.fingerprint(
                source_network, purpose="network"
            ),
            user_agent_fingerprint=self.fingerprint(
                user_agent, purpose="user-agent"
            ),
        )
        await self._call(
            "hset",
            f"{PREAUTH_KEY_PREFIX}{context_ref}",
            mapping={
                "context_ref": context_ref,
                "created_at_epoch": now,
                "expires_at_epoch": context.expires_at_epoch,
                "csrf_generation": 1,
                "source_network_fingerprint": (
                    context.source_network_fingerprint or ""
                ),
                "user_agent_fingerprint": context.user_agent_fingerprint or "",
            },
        )
        await self._call(
            "expireat",
            f"{PREAUTH_KEY_PREFIX}{context_ref}",
            context.expires_at_epoch,
        )
        return CreatedPreAuthContext(token=token, context=context)

    async def get_preauth(
        self,
        token: str,
        *,
        source_network: str | None,
        user_agent: str | None,
        now_epoch: int | None = None,
    ) -> PreAuthContext | None:
        self._validate_token(token)
        context_ref = self._digest("preauth", token)
        key = f"{PREAUTH_KEY_PREFIX}{context_ref}"
        values = await self._call("hgetall", key)
        if not values:
            return None
        try:
            context = PreAuthContext(
                context_ref=values["context_ref"],
                created_at_epoch=int(values["created_at_epoch"]),
                expires_at_epoch=int(values["expires_at_epoch"]),
                csrf_generation=int(values["csrf_generation"]),
                source_network_fingerprint=(
                    values.get("source_network_fingerprint") or None
                ),
                user_agent_fingerprint=(
                    values.get("user_agent_fingerprint") or None
                ),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise SessionStoreUnavailable("pre-authentication state is malformed") from exc
        now = int(self._clock()) if now_epoch is None else int(now_epoch)
        if context.expires_at_epoch <= now:
            await self._call("delete", key)
            return None
        expected_network = self.fingerprint(source_network, purpose="network")
        expected_agent = self.fingerprint(user_agent, purpose="user-agent")
        if (
            context.source_network_fingerprint != expected_network
            or context.user_agent_fingerprint != expected_agent
        ):
            return None
        return context

    async def consume_preauth(self, token: str) -> None:
        self._validate_token(token)
        await self._call(
            "delete",
            f"{PREAUTH_KEY_PREFIX}{self._digest('preauth', token)}",
        )

    def validate_csrf(self, supplied: str | None, expected: str) -> bool:
        return bool(supplied) and hmac.compare_digest(supplied, expected)

    def rate_key(self, dimension: str, value: str) -> str:
        return f"{RATE_KEY_PREFIX}{dimension}:{self._digest(f'rate-{dimension}', value)}"

    async def rate_count(self, dimension: str, value: str) -> int:
        raw = await self._call("get", self.rate_key(dimension, value))
        return int(raw or 0)

    async def record_rate_failure(self, dimension: str, value: str) -> int:
        key = self.rate_key(dimension, value)
        script = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return count
"""
        result = await self._call(
            "eval",
            script,
            1,
            key,
            self.settings.browser_auth_rate_window_seconds,
        )
        return int(result)

    async def clear_rate(self, dimension: str, value: str) -> None:
        await self._call("delete", self.rate_key(dimension, value))
