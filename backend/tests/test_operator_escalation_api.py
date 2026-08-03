from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError

from app.api.browser_auth_deps import get_browser_redis, get_browser_settings
from app.api.browser_auth_errors import (
    BrowserAuthError,
    browser_error_response,
    browser_validation_error_response,
)
from app.api.v1 import operator_conversations
from app.config import Settings
from app.database import get_db
from app.models.escalation_ticket import EscalationTicket
from app.models.operator_account import OperatorAccount
from app.modules.m8_maps.operator_escalation import (
    EscalationAlreadyOpen,
    IdempotencyConflict,
    IdempotencyInProgress,
    OperatorEscalationResult,
    OperatorEscalationUnavailable,
)
from app.operator_identity.browser_auth import BrowserAuthState, SESSION_COOKIE_NAME
from app.operator_identity.passwords import hash_password
from app.security import create_access_token

ORIGIN = "https://operator.example"
PASSWORD = "Correct-Horse-9"
VALID_BODY = {
    "reason": "Customer explicitly requested specialist assistance.",
    "type": "complex_issue",
    "priority": "medium",
}


def _settings() -> Settings:
    return Settings(
        browser_auth_enabled=True,
        browser_allowed_origin=ORIGIN,
        browser_session_hmac_secret="s" * 32,
        browser_csrf_hmac_secret="c" * 32,
        browser_idempotency_hmac_secret="i" * 32,
        browser_session_redis_db=4,
        browser_session_activity_coalesce_seconds=0,
    )


def _account(
    *,
    role: str = "operator",
    account_status: str = "active",
) -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized=f"{role}.e2",
        display_name=f"{role.title()} E2",
        email_normalized=None,
        password_hash=hash_password(PASSWORD),
        role=role,
        status=account_status,
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


class FakeDatabase:
    def __init__(self, account: OperatorAccount) -> None:
        self.account = account
        self.scalar_count = 0

    async def scalar(self, _statement):
        self.scalar_count += 1
        return self.account

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def _ticket(
    conversation_id: uuid.UUID,
    account_id: uuid.UUID,
) -> EscalationTicket:
    return EscalationTicket(
        ticket_id=uuid.uuid4(),
        conversation_id=conversation_id,
        customer_id="+243812345678",
        priority="medium",
        reason="complex_complaint",
        source="operator_browser",
        escalation_type="complex_issue",
        operator_reason=VALID_BODY["reason"],
        created_by_account_id=account_id,
        status="open",
        transcript_snapshot=[],
        created_at=datetime.now(timezone.utc),
    )


def _app(
    settings: Settings,
    redis_client: Any,
    database: FakeDatabase,
) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(BrowserAuthError, browser_error_response)
    app.add_exception_handler(
        RequestValidationError, browser_validation_error_response
    )
    app.include_router(operator_conversations.router, prefix="/api/v1")
    app.dependency_overrides[get_browser_settings] = lambda: settings
    app.dependency_overrides[get_browser_redis] = lambda: redis_client

    async def _db_override():
        yield database

    app.dependency_overrides[get_db] = _db_override
    return app


async def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    role: str = "operator",
    account_status: str = "active",
    replayed: bool = False,
) -> tuple[
    httpx.AsyncClient,
    FakeDatabase,
    OperatorAccount,
    fakeredis.aioredis.FakeRedis,
    str,
    list[dict[str, Any]],
]:
    settings = _settings()
    account = _account(role=role, account_status=account_status)
    database = FakeDatabase(account)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = BrowserAuthState(redis_client=redis_client, settings=settings)
    created = await state.sessions.create_session(
        account_id=account.account_id,
        auth_version=account.auth_version,
        ip_prefix=state.source_network("127.0.0.1"),
        user_agent="python-httpx/0.27.2",
    )
    calls: list[dict[str, Any]] = []

    async def _create(_db, **kwargs):
        calls.append(kwargs)
        return OperatorEscalationResult(
            ticket=_ticket(kwargs["conversation_id"], account.account_id),
            actor_display_name=account.display_name,
            replayed=replayed,
        )

    monkeypatch.setattr(
        operator_conversations, "create_operator_escalation", _create
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_app(settings, redis_client, database),
            client=("127.0.0.1", 12345),
            raise_app_exceptions=False,
        ),
        base_url=ORIGIN,
    )
    client.cookies.set(SESSION_COOKIE_NAME, created.token)
    csrf = state.csrf_for_session(created.record)
    return client, database, account, redis_client, csrf, calls


def _headers(csrf: str, key: uuid.UUID | None = None) -> dict[str, str]:
    return {
        "Idempotency-Key": str(key or uuid.uuid4()),
        "X-CSRF-Token": csrf,
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "X-Request-ID": "e2-api-request",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_active_operator_roles_create_with_real_actor(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    client, database, account, redis_client, csrf, calls = await _client(
        monkeypatch, role=role
    )
    conversation_id = uuid.uuid4()
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/escalations",
            json={**VALID_BODY, "reason": f"  {VALID_BODY['reason']}  "},
            headers=_headers(csrf),
        )
        assert response.status_code == 201
        assert response.headers["idempotent-replayed"] == "false"
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["conversation_id"] == str(conversation_id)
        assert response.json()["created_by"] == {
            "account_id": str(account.account_id),
            "display_name": account.display_name,
        }
        assert calls[0]["actor_account_id"] == account.account_id
        assert calls[0]["actor_role"] == role
        assert calls[0]["reason"] == VALID_BODY["reason"]
        assert calls[0]["request_id"] == "e2-api-request"
        assert database.scalar_count == 1
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_exact_replay_is_200_with_same_escalation_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account_value, redis_client, csrf, _calls = (
        await _client(monkeypatch, replayed=True)
    )
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers=_headers(csrf),
        )
        assert response.status_code == 200
        assert response.headers["idempotent-replayed"] == "true"
        assert response.json()["status"] == "open"
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_error", "code"),
    [
        (IdempotencyConflict, "IDEMPOTENCY_CONFLICT"),
        (IdempotencyInProgress, "IDEMPOTENCY_IN_PROGRESS"),
        (EscalationAlreadyOpen, "ESCALATION_ALREADY_OPEN"),
    ],
)
async def test_conflicts_have_stable_operator_codes(
    monkeypatch: pytest.MonkeyPatch,
    service_error: type[Exception],
    code: str,
) -> None:
    client, _database, _account_value, redis_client, csrf, _calls = (
        await _client(monkeypatch)
    )

    async def _fail(*_args, **_kwargs):
        raise service_error

    monkeypatch.setattr(
        operator_conversations, "create_operator_escalation", _fail
    )
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers=_headers(csrf),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == code
        assert VALID_BODY["reason"] not in response.text
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_unavailable_error_is_stable_and_non_sensitive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account_value, redis_client, csrf, _calls = (
        await _client(monkeypatch)
    )

    async def _fail(*_args, **_kwargs):
        raise OperatorEscalationUnavailable("private database detail")

    monkeypatch.setattr(
        operator_conversations, "create_operator_escalation", _fail
    )
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers=_headers(csrf),
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
        assert "private database detail" not in response.text
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_analyst_service_jwt_disabled_and_expired_sessions_are_denied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    analyst, _db, _account_value, analyst_redis, csrf, calls = await _client(
        monkeypatch, role="analyst"
    )
    try:
        denied = await analyst.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers=_headers(csrf),
        )
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "FORBIDDEN"
        assert calls == []
    finally:
        await analyst.aclose()
        await analyst_redis.aclose()

    jwt_client, _db, _account_value, jwt_redis, csrf, calls = await _client(
        monkeypatch
    )
    jwt_client.cookies.clear()
    token = create_access_token("service", "admin")
    try:
        denied = await jwt_client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers={
                **_headers(csrf),
                "Authorization": f"Bearer {token}",
            },
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"
        assert calls == []
    finally:
        await jwt_client.aclose()
        await jwt_redis.aclose()

    disabled, _db, _account_value, disabled_redis, csrf, calls = await _client(
        monkeypatch, account_status="disabled"
    )
    try:
        denied = await disabled.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers=_headers(csrf),
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTH_ACCOUNT_DISABLED"
        assert calls == []
    finally:
        await disabled.aclose()
        await disabled_redis.aclose()

    expired, _db, _account_value, expired_redis, _csrf, calls = await _client(
        monkeypatch
    )
    expired.cookies.clear()
    expired.cookies.set(SESSION_COOKIE_NAME, BrowserAuthState.generate_token())
    try:
        denied = await expired.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations",
            json=VALID_BODY,
            headers=_headers("invalid"),
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"
        assert calls == []
    finally:
        await expired.aclose()
        await expired_redis.aclose()


@pytest.mark.asyncio
async def test_csrf_origin_key_and_body_validation_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _database, _account_value, redis_client, csrf, calls = (
        await _client(monkeypatch)
    )
    path = f"/api/v1/operator/conversations/{uuid.uuid4()}/escalations"
    try:
        missing_csrf = await client.post(
            path,
            json=VALID_BODY,
            headers={
                key: value
                for key, value in _headers(csrf).items()
                if key != "X-CSRF-Token"
            },
        )
        assert missing_csrf.status_code == 403

        wrong_origin = await client.post(
            path,
            json=VALID_BODY,
            headers={**_headers(csrf), "Origin": "https://attacker.example"},
        )
        assert wrong_origin.status_code == 403

        invalid_key = await client.post(
            path,
            json=VALID_BODY,
            headers={**_headers(csrf), "Idempotency-Key": str(uuid.uuid1())},
        )
        assert invalid_key.status_code == 422
        assert invalid_key.json()["error"]["code"] == "VALIDATION_ERROR"

        for body in (
            {**VALID_BODY, "reason": " too short "},
            {**VALID_BODY, "reason": "x" * 501},
            {**VALID_BODY, "type": "unapproved"},
            {**VALID_BODY, "priority": "urgent"},
        ):
            invalid = await client.post(
                path, json=body, headers=_headers(csrf)
            )
            assert invalid.status_code == 422
            assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
        assert calls == []
    finally:
        await client.aclose()
        await redis_client.aclose()
