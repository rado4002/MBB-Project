from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.exc import SQLAlchemyError

from app.api.browser_auth_deps import (
    get_browser_redis,
    get_browser_settings,
)
from app.api.browser_auth_errors import (
    BrowserAuthError,
    browser_error_response,
    browser_validation_error_response,
)
from app.api.deps import get_current_role
from app.api.v1 import auth
from app.config import Settings
from app.database import get_db
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import OperatorAuditEvent
from app.operator_identity.browser_auth import (
    PREAUTH_COOKIE_NAME,
    SESSION_COOKIE_NAME,
)
from app.operator_identity.passwords import hash_password
from app.request_ids import normalize_or_generate_request_id
from app.schemas.auth import LoginRequest
from app.security import create_access_token

ORIGIN = "https://operator.example"
PASSWORD = "Correct-Horse-9"
NEW_PASSWORD = "Better-Browser-84!"


def _settings(**overrides: Any) -> Settings:
    values = {
        "browser_auth_enabled": True,
        "browser_allowed_origin": ORIGIN,
        "browser_session_hmac_secret": "s" * 32,
        "browser_csrf_hmac_secret": "c" * 32,
        "browser_session_redis_db": 4,
        "browser_session_idle_seconds": 1800,
        "browser_session_absolute_seconds": 28800,
        "browser_recent_reauth_seconds": 600,
        "browser_max_sessions_per_account": 2,
        "browser_session_activity_coalesce_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _account(*, disabled: bool = False, temporary: bool = False) -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="operator.one",
        display_name="Operator One",
        email_normalized=None,
        password_hash=hash_password(PASSWORD),
        role="operator",
        status="disabled" if disabled else "active",
        auth_version=1,
        must_change_password=temporary,
        temporary_password_expires_at=now + timedelta(hours=1) if temporary else None,
        password_changed_at=None,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


class FakeDatabase:
    def __init__(
        self,
        account: OperatorAccount | None,
        *,
        scalar_error: Exception | None = None,
    ) -> None:
        self.account = account
        self.scalar_error = scalar_error
        self.added: list[Any] = []
        self.commit_count = 0

    async def scalar(self, _statement):
        if self.scalar_error:
            raise self.scalar_error
        return self.account

    def add(self, value: Any) -> None:
        self.added.append(value)

    async def flush(self) -> None:
        for value in self.added:
            if isinstance(value, OperatorAuditEvent) and value.event_id is None:
                value.event_id = uuid.uuid4()

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        return None


class UnavailableRedis:
    def __getattr__(self, _name: str):
        async def _unavailable(*_args, **_kwargs):
            raise ConnectionError("private redis failure")

        return _unavailable


def _app(settings: Settings, redis_client: Any, database: FakeDatabase) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(BrowserAuthError, browser_error_response)
    app.add_exception_handler(
        RequestValidationError, browser_validation_error_response
    )
    app.include_router(auth.router, prefix="/api/v1")
    app.dependency_overrides[get_browser_settings] = lambda: settings
    app.dependency_overrides[get_browser_redis] = lambda: redis_client

    async def _db_override():
        yield database

    app.dependency_overrides[get_db] = _db_override

    @app.get("/legacy")
    async def legacy(role: str = Depends(get_current_role)):
        return {"role": role}

    return app


@pytest_asyncio.fixture(loop_scope="function")
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.flushall()
    await client.aclose()


@pytest_asyncio.fixture(loop_scope="function")
async def harness(redis_client):
    account = _account()
    database = FakeDatabase(account)
    app = _app(_settings(), redis_client, database)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        yield client, account, database, redis_client


async def _csrf(client: httpx.AsyncClient) -> tuple[str, str]:
    response = await client.get("/api/v1/auth/csrf")
    assert response.status_code == 200
    token = response.json()["csrf_token"]
    preauth = client.cookies.get(PREAUTH_COOKIE_NAME)
    assert preauth
    return token, preauth


def _headers(csrf_token: str) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf_token,
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }


async def _login(
    client: httpx.AsyncClient,
    *,
    password: str = PASSWORD,
) -> httpx.Response:
    csrf_token, _ = await _csrf(client)
    return await client.post(
        "/api/v1/auth/login",
        headers=_headers(csrf_token),
        json={"username": "Operator.One", "password": password},
    )


@pytest.mark.asyncio
async def test_preauth_csrf_cookie_and_valid_login_are_opaque(harness) -> None:
    client, _account_value, database, redis_client = harness
    csrf_token, preauth = await _csrf(client)
    csrf_response = await client.get("/api/v1/auth/csrf")
    set_cookie = csrf_response.headers.get("set-cookie", "")
    assert "Cache-Control" in csrf_response.headers
    assert PREAUTH_COOKIE_NAME in set_cookie
    assert "Secure" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert "Path=/" in set_cookie
    assert "Domain=" not in set_cookie

    response = await client.post(
        "/api/v1/auth/login",
        headers=_headers(csrf_response.json()["csrf_token"]),
        json={"username": " Operator.One ", "password": PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["human"]["username"] == "operator.one"
    assert body["must_change_password"] is False
    assert body["capabilities"] == sorted(
        [
            "auth.csrf.read",
            "auth.logout",
            "auth.password.change",
            "auth.reauthenticate",
            "auth.session.read",
        ]
    )
    assert body["csrf_token"]
    session_token = client.cookies.get(SESSION_COOKIE_NAME)
    assert session_token and session_token != preauth
    assert session_token not in response.text
    assert "session_ref" not in response.text
    assert client.cookies.get(PREAUTH_COOKIE_NAME) is None
    session_cookie = response.headers["set-cookie"]
    assert "__Host-mbb_session=" in session_cookie
    assert "Secure" in session_cookie and "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie and "Path=/" in session_cookie
    assert "Domain=" not in session_cookie
    assert database.commit_count == 1
    values: list[str] = []
    for key in await redis_client.keys("*"):
        if await redis_client.type(key) == "hash":
            values.extend((await redis_client.hgetall(key)).values())
    assert session_token not in values
    assert csrf_token not in values


@pytest.mark.asyncio
async def test_generic_invalid_and_disabled_login(harness, redis_client) -> None:
    client, account, _database, _redis = harness
    wrong = await _login(client, password="Wrong-Password-8")
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "invalid_credentials"
    assert "operator" not in wrong.text.lower()
    account.status = "disabled"
    disabled = await _login(client)
    assert disabled.status_code == 401
    assert disabled.json()["error"]["code"] == "invalid_credentials"


@pytest.mark.asyncio
async def test_csrf_origin_fetch_metadata_and_json_are_enforced(harness) -> None:
    client, *_ = harness
    csrf_token, _ = await _csrf(client)
    bad_csrf = await client.post(
        "/api/v1/auth/login",
        headers=_headers("wrong"),
        json={"username": "operator.one", "password": PASSWORD},
    )
    assert bad_csrf.status_code == 403
    bad_origin_headers = _headers(csrf_token)
    bad_origin_headers["Origin"] = "https://attacker.example"
    bad_origin = await client.post(
        "/api/v1/auth/login",
        headers=bad_origin_headers,
        json={"username": "operator.one", "password": PASSWORD},
    )
    assert bad_origin.status_code == 403
    cross_site_headers = _headers(csrf_token)
    cross_site_headers["Sec-Fetch-Site"] = "cross-site"
    cross_site = await client.post(
        "/api/v1/auth/login",
        headers=cross_site_headers,
        json={"username": "operator.one", "password": PASSWORD},
    )
    assert cross_site.status_code == 403
    not_json = await client.post(
        "/api/v1/auth/login",
        headers={
            "X-CSRF-Token": csrf_token,
            "Origin": ORIGIN,
            "Content-Type": "text/plain",
        },
        content="not-json",
    )
    assert not_json.status_code == 422
    assert not_json.json()["error"]["code"] == "request_invalid"
    referer_fallback = await client.post(
        "/api/v1/auth/login",
        headers={
            "X-CSRF-Token": csrf_token,
            "Referer": f"{ORIGIN}/operator/login",
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
        json={"username": "operator.one", "password": PASSWORD},
    )
    assert referer_fallback.status_code == 200


@pytest.mark.asyncio
async def test_session_csrf_logout_and_repeated_logout(harness) -> None:
    client, _account_value, database, _redis = harness
    login = await _login(client)
    assert login.status_code == 200
    old_csrf = login.json()["csrf_token"]
    session = await client.get("/api/v1/auth/session")
    assert session.status_code == 200
    assert "csrf_token" not in session.json()
    csrf = await client.get("/api/v1/auth/csrf")
    assert csrf.status_code == 200
    assert csrf.json()["csrf_token"] == old_csrf
    rejected = await client.post(
        "/api/v1/auth/logout", headers=_headers("wrong"), json={}
    )
    assert rejected.status_code == 403
    assert client.cookies.get(SESSION_COOKIE_NAME)
    logout = await client.post(
        "/api/v1/auth/logout", headers=_headers(old_csrf), json={}
    )
    assert logout.status_code == 200
    assert client.cookies.get(SESSION_COOKIE_NAME) is None
    repeated = await client.post(
        "/api/v1/auth/logout",
        headers={
            "Origin": ORIGIN,
            "Sec-Fetch-Site": "same-origin",
            "Content-Type": "application/json",
        },
        json={},
    )
    assert repeated.status_code == 200
    assert any(
        isinstance(event, OperatorAuditEvent)
        and event.action == "browser_logout"
        and event.outcome == "succeeded"
        for event in database.added
    )


@pytest.mark.asyncio
async def test_reauthentication_rotates_token_and_csrf(harness) -> None:
    client, _account_value, database, _redis = harness
    login = await _login(client)
    old_token = client.cookies.get(SESSION_COOKIE_NAME)
    old_csrf = login.json()["csrf_token"]
    absolute_expiry = login.json()["absolute_expires_at_epoch"]
    response = await client.post(
        "/api/v1/auth/reauthenticate",
        headers=_headers(old_csrf),
        json={"password": PASSWORD},
    )
    assert response.status_code == 200
    assert client.cookies.get(SESSION_COOKIE_NAME) != old_token
    assert response.json()["csrf_token"] != old_csrf
    assert response.json()["absolute_expires_at_epoch"] == absolute_expiry
    assert any(
        isinstance(event, OperatorAuditEvent)
        and event.action == "browser_reauthentication"
        for event in database.added
    )


@pytest.mark.asyncio
async def test_temporary_session_is_restricted_until_password_change(redis_client) -> None:
    account = _account(temporary=True)
    database = FakeDatabase(account)
    app = _app(_settings(), redis_client, database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        login = await _login(client)
        assert login.status_code == 200
        assert login.json()["must_change_password"] is True
        assert login.json()["capabilities"] == sorted(
            [
                "auth.csrf.read",
                "auth.logout",
                "auth.password.change",
                "auth.session.read",
            ]
        )
        old_token = client.cookies.get(SESSION_COOKIE_NAME)
        changed = await client.post(
            "/api/v1/auth/password/change",
            headers=_headers(login.json()["csrf_token"]),
            json={
                "current_password": PASSWORD,
                "new_password": NEW_PASSWORD,
            },
        )
        assert changed.status_code == 200
        assert changed.json()["must_change_password"] is False
        assert "auth.reauthenticate" in changed.json()["capabilities"]
        assert client.cookies.get(SESSION_COOKIE_NAME) != old_token
        assert account.auth_version == 2
        assert account.temporary_password_expires_at is None
        assert PASSWORD not in repr(account)
        assert NEW_PASSWORD not in repr(account)
        assert any(
            isinstance(event, OperatorAuditEvent)
            and event.action == "browser_password_changed"
            for event in database.added
        )


@pytest.mark.asyncio
async def test_password_change_uses_central_policy_and_rejects_reuse(redis_client) -> None:
    account = _account(temporary=True)
    app = _app(_settings(), redis_client, FakeDatabase(account))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        login = await _login(client)
        for new_password in (
            "short",
            PASSWORD,
            "safe Operator_One suffix",
            "Correct Horse Battery Staple",
        ):
            rejected = await client.post(
                "/api/v1/auth/password/change",
                headers=_headers(login.json()["csrf_token"]),
                json={
                    "current_password": PASSWORD,
                    "new_password": new_password,
                },
            )
            assert rejected.status_code == 422
            assert rejected.json()["error"]["code"] == "password_policy_violation"
        assert account.must_change_password is True
        assert account.auth_version == 1


@pytest.mark.asyncio
async def test_auth_version_and_disabled_account_revoke_existing_session(harness) -> None:
    client, account, *_ = harness
    assert (await _login(client)).status_code == 200
    account.auth_version += 1
    revoked = await client.get("/api/v1/auth/session")
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "session_invalid"


@pytest.mark.asyncio
async def test_account_disablement_revokes_existing_session(harness) -> None:
    client, account, *_ = harness
    assert (await _login(client)).status_code == 200
    account.status = "disabled"
    revoked = await client.get("/api/v1/auth/session")
    assert revoked.status_code == 401
    assert revoked.json()["error"]["code"] == "session_invalid"


@pytest.mark.asyncio
async def test_two_session_integration_evicts_oldest(redis_client) -> None:
    account = _account()
    database = FakeDatabase(account)
    app = _app(_settings(), redis_client, database)
    clients = [
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url=ORIGIN
        )
        for _ in range(3)
    ]
    try:
        for client in clients:
            assert (await _login(client)).status_code == 200
        assert (await clients[0].get("/api/v1/auth/session")).status_code == 401
        assert (await clients[1].get("/api/v1/auth/session")).status_code == 200
        assert (await clients[2].get("/api/v1/auth/session")).status_code == 200
        successful_logins = [
            event
            for event in database.added
            if isinstance(event, OperatorAuditEvent)
            and event.action == "browser_login"
        ]
        assert successful_logins[-1].event_metadata["oldest_session_evicted"] is True
    finally:
        for client in clients:
            await client.aclose()


@pytest.mark.asyncio
async def test_login_rate_limit_and_audit(harness) -> None:
    client, _account_value, database, _redis = harness
    for _ in range(5):
        response = await _login(client, password="Wrong-Password-8")
        assert response.status_code == 401
    throttled = await _login(client, password=PASSWORD)
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "authentication_throttled"
    assert throttled.headers["retry-after"] == "900"
    actions = [
        event.action
        for event in database.added
        if isinstance(event, OperatorAuditEvent)
    ]
    assert "browser_login_failed" in actions
    assert "browser_login_throttled" in actions


@pytest.mark.asyncio
async def test_reauthentication_failure_limit_is_per_session(harness) -> None:
    client, *_ = harness
    login = await _login(client)
    assert login.status_code == 200
    csrf_token = login.json()["csrf_token"]
    for _ in range(5):
        failed = await client.post(
            "/api/v1/auth/reauthenticate",
            headers=_headers(csrf_token),
            json={"password": "Wrong-Password-8"},
        )
        assert failed.status_code == 401
    throttled = await client.post(
        "/api/v1/auth/reauthenticate",
        headers=_headers(csrf_token),
        json={"password": PASSWORD},
    )
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "authentication_throttled"


@pytest.mark.asyncio
async def test_redis_and_database_failures_are_typed_and_fail_closed(redis_client) -> None:
    account = _account()
    redis_app = _app(_settings(), UnavailableRedis(), FakeDatabase(account))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=redis_app), base_url=ORIGIN
    ) as client:
        response = await client.get("/api/v1/auth/csrf")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "authentication_unavailable"
        assert "private redis failure" not in response.text

    database = FakeDatabase(account, scalar_error=SQLAlchemyError("private db failure"))
    database_app = _app(_settings(), redis_client, database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=database_app), base_url=ORIGIN
    ) as client:
        csrf_token, _ = await _csrf(client)
        response = await client.post(
            "/api/v1/auth/login",
            headers=_headers(csrf_token),
            json={"username": "operator.one", "password": PASSWORD},
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "authentication_unavailable"
        assert "private db failure" not in response.text


@pytest.mark.asyncio
async def test_browser_auth_default_off_no_jwt_fallback_and_legacy_jwt_works(
    redis_client,
) -> None:
    database = FakeDatabase(_account())
    app = _app(_settings(browser_auth_enabled=False), redis_client, database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=ORIGIN
    ) as client:
        token = create_access_token("dashboard", "admin")
        browser = await client.get(
            "/api/v1/auth/session",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert browser.status_code == 503
        assert browser.json()["error"]["code"] == "browser_auth_disabled"
        legacy = await client.get(
            "/legacy", headers={"Authorization": f"Bearer {token}"}
        )
        assert legacy.status_code == 200
        assert legacy.json() == {"role": "admin"}


@pytest.mark.asyncio
async def test_browser_cookie_does_not_satisfy_legacy_jwt(harness) -> None:
    client, *_ = harness
    assert (await _login(client)).status_code == 200
    legacy = await client.get("/legacy")
    assert legacy.status_code == 401
    assert legacy.json()["detail"] == "missing_token"


def test_request_id_is_strict_and_passwords_are_secret_fields() -> None:
    assert normalize_or_generate_request_id("trace_123") == "trace_123"
    generated = normalize_or_generate_request_id("bad request id\ninjected")
    assert re.fullmatch(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
        generated,
    )
    request = LoginRequest(username="operator.one", password="not-logged")
    assert "not-logged" not in repr(request)
