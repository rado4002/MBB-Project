from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    BrowserSessionContext,
    get_browser_redis,
    get_browser_settings,
)
from app.api.browser_auth_errors import (
    BrowserAuthError,
    browser_error_response,
    browser_validation_error_response,
)
from app.api.deps import get_current_role
from app.api.v1 import operator_conversations
from app.config import Settings
from app.database import get_db
from app.models.operator_account import OperatorAccount
from app.main import unhandled_exception_handler
from app.middleware import MaintenanceModeMiddleware, RequestTracingMiddleware
from app.operator_identity.browser_auth import BrowserAuthState, SESSION_COOKIE_NAME
from app.operator_identity.passwords import hash_password
from app.security import create_access_token

ORIGIN = "https://operator.example"
FULL_PHONE = "+243812345678"
PASSWORD = "Correct-Horse-9"


def _settings() -> Settings:
    return Settings(
        browser_auth_enabled=True,
        browser_allowed_origin=ORIGIN,
        browser_session_hmac_secret="s" * 32,
        browser_csrf_hmac_secret="c" * 32,
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
        username_normalized=f"{role}.one",
        display_name=f"{role.title()} One",
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


class FakeMappingResult:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def mappings(self) -> FakeMappingResult:
        return self

    def all(self) -> list[dict[str, Any]]:
        return self.rows

    def one_or_none(self) -> dict[str, Any] | None:
        if not self.rows:
            return None
        assert len(self.rows) == 1
        return self.rows[0]


class FakeReadDatabase:
    def __init__(self, account: OperatorAccount) -> None:
        self.account = account
        self.queue_rows: list[dict[str, Any]] = []
        self.detail_rows: list[dict[str, Any]] = []
        self.message_rows: list[dict[str, Any]] = []
        self.conversation_accessible = True
        self.scalar_count = 0
        self.execute_count = 0
        self.statements: list[Any] = []
        self.execute_error: Exception | None = None

    async def scalar(self, statement):
        self.scalar_count += 1
        self.statements.append(statement)
        sql = str(statement)
        if "operator_accounts" in sql:
            return self.account
        return uuid.uuid4() if self.conversation_accessible else None

    async def execute(self, statement) -> FakeMappingResult:
        if self.execute_error is not None:
            raise self.execute_error
        self.execute_count += 1
        self.statements.append(statement)
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "JOIN LATERAL" in sql:
            return FakeMappingResult(self.queue_rows)
        if "LEFT OUTER JOIN mbb.leads" in sql:
            return FakeMappingResult(self.detail_rows)
        if "FROM mbb.messages" in sql:
            return FakeMappingResult(self.message_rows)
        raise AssertionError(f"Unexpected E1 query: {sql}")

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None

    def reset_counts(self) -> None:
        self.scalar_count = 0
        self.execute_count = 0
        self.statements.clear()


def _app(
    settings: Settings,
    redis_client: Any,
    database: FakeReadDatabase,
) -> FastAPI:
    app = FastAPI()
    app.add_exception_handler(BrowserAuthError, browser_error_response)
    app.add_exception_handler(
        RequestValidationError, browser_validation_error_response
    )
    app.add_exception_handler(Exception, unhandled_exception_handler)
    app.include_router(operator_conversations.router, prefix="/api/v1")
    app.dependency_overrides[get_browser_settings] = lambda: settings
    app.dependency_overrides[get_browser_redis] = lambda: redis_client

    async def _db_override():
        yield database

    app.dependency_overrides[get_db] = _db_override

    @app.get("/legacy")
    async def legacy(role: str = Depends(get_current_role)):
        return {"role": role}

    return app


async def _authenticated_client(
    *,
    role: str = "operator",
    account_status: str = "active",
) -> tuple[
    httpx.AsyncClient,
    FakeReadDatabase,
    OperatorAccount,
    fakeredis.aioredis.FakeRedis,
]:
    settings = _settings()
    account = _account(role=role, account_status=account_status)
    database = FakeReadDatabase(account)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = BrowserAuthState(redis_client=redis_client, settings=settings)
    created = await state.sessions.create_session(
        account_id=account.account_id,
        auth_version=account.auth_version,
        ip_prefix=state.source_network("127.0.0.1"),
        user_agent="python-httpx/0.27.2",
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
    return client, database, account, redis_client


@pytest_asyncio.fixture
async def operator_harness():
    client, database, account, redis_client = await _authenticated_client()
    try:
        yield client, database, account
    finally:
        await client.aclose()
        await redis_client.aclose()


def _queue_row(
    *,
    conversation_id: uuid.UUID,
    occurred_at: datetime,
    direction: str = "inbound",
    content: str = "Bonjour",
    content_type: str = "text",
    language: str = "french",
    open_escalation: bool = False,
) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "last_message_time": occurred_at,
        "language_detected": language,
        "status": "active",
        "message_count": 2,
        "owner_type": "ai",
        "human_owner_account_id": None,
        "human_owner_display_name": None,
        "ai_execution_state": "eligible",
        "ownership_version": 1,
        "ownership_updated_at": occurred_at,
        "customer_display_name": "<script>alert(1)</script>",
        "customer_phone_masked": "***5678",
        "latest_content": content,
        "latest_content_type": content_type,
        "latest_direction": direction,
        "latest_occurred_at": occurred_at,
        "has_open_escalation": open_escalation,
    }


def _detail_row(conversation_id: uuid.UUID, now: datetime) -> dict[str, Any]:
    return {
        "conversation_id": conversation_id,
        "status": "qualifying",
        "language_detected": "lingala",
        "message_count": 7,
        "updated_at": now,
        "owner_type": "ai",
        "human_owner_account_id": None,
        "human_owner_display_name": None,
        "ai_execution_state": "eligible",
        "ownership_version": 1,
        "ownership_updated_at": now,
        "customer_display_name": "Cliente",
        "customer_phone_masked": "***5678",
        "lead_score": "hot",
        "lead_stage": "consideration",
        "lead_intent": "product_inquiry",
        "lead_product_interests": [
            "A" * 100,
            "B",
            "C",
            "D",
            "E",
            "F",
        ],
        "has_open_escalation": True,
    }


def _message_row(
    *,
    message_id: uuid.UUID,
    occurred_at: datetime,
    direction: str,
    content: str,
    content_type: str = "text",
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "occurred_at": occurred_at,
        "direction": direction,
        "content_type": content_type,
        "content": content,
        "language": "french",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["operator", "administrator"])
async def test_operator_and_administrator_can_read_queue(role: str) -> None:
    client, database, _account_value, redis_client = await _authenticated_client(
        role=role
    )
    database.queue_rows = []
    try:
        response = await client.get("/api/v1/operator/conversations")
        assert response.status_code == 200
        assert response.json() == {"items": [], "next_cursor": None}
        assert database.scalar_count == 1
        assert database.execute_count == 1
    finally:
        await client.aclose()
        await redis_client.aclose()


@pytest.mark.asyncio
async def test_analyst_jwt_and_disabled_account_are_denied() -> None:
    analyst, analyst_db, _account_value, analyst_redis = (
        await _authenticated_client(role="analyst")
    )
    try:
        denied = await analyst.get("/api/v1/operator/conversations")
        assert denied.status_code == 403
        assert denied.json()["error"]["code"] == "FORBIDDEN"
        assert analyst_db.execute_count == 0
    finally:
        await analyst.aclose()
        await analyst_redis.aclose()

    jwt_client, jwt_db, _account_value, jwt_redis = await _authenticated_client()
    jwt_client.cookies.clear()
    token = create_access_token("dashboard", "admin")
    try:
        denied = await jwt_client.get(
            "/api/v1/operator/conversations",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTH_SESSION_EXPIRED"
        assert jwt_db.scalar_count == 0
        assert jwt_db.execute_count == 0
        legacy = await jwt_client.get(
            "/legacy", headers={"Authorization": f"Bearer {token}"}
        )
        assert legacy.status_code == 200
    finally:
        await jwt_client.aclose()
        await jwt_redis.aclose()

    disabled, disabled_db, _account_value, disabled_redis = (
        await _authenticated_client(account_status="disabled")
    )
    try:
        denied = await disabled.get("/api/v1/operator/conversations")
        assert denied.status_code == 401
        assert denied.json()["error"]["code"] == "AUTH_ACCOUNT_DISABLED"
        assert disabled_db.execute_count == 0
    finally:
        await disabled.aclose()
        await disabled_redis.aclose()


@pytest.mark.asyncio
async def test_queue_is_minimized_masked_bounded_and_stably_cursor_paginated(
    operator_harness,
) -> None:
    client, database, _account_value = operator_harness
    tied = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    ids = sorted([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()], reverse=True)
    hostile = "🙂" * 121 + "<script>alert(document.cookie)</script>"
    database.queue_rows = [
        _queue_row(
            conversation_id=ids[0],
            occurred_at=tied,
            content=hostile,
            open_escalation=True,
        ),
        _queue_row(
            conversation_id=ids[1],
            occurred_at=tied,
            direction="outbound",
            content="provider text",
        ),
        _queue_row(
            conversation_id=ids[2],
            occurred_at=tied - timedelta(minutes=1),
            content_type="voice_note",
            content="https://provider.example/private-media",
            language="swahili",
        ),
    ]

    first = await client.get(
        "/api/v1/operator/conversations",
        params={
            "limit": 2,
            "status": "active",
            "escalation_state": "open",
            "language": "french",
        },
    )
    assert first.status_code == 200
    body = first.json()
    assert [item["conversation_id"] for item in body["items"]] == [
        str(ids[0]),
        str(ids[1]),
    ]
    assert len(body["items"][0]["latest_message"]["preview"]) == 120
    assert body["items"][0]["awaiting_response_since"] is not None
    assert body["items"][1]["awaiting_response_since"] is None
    assert body["items"][0]["ownership"] == {
        "owner_type": "ai",
        "human_owner": None,
        "ai_execution_state": "eligible",
        "version": 1,
        "updated_at": tied.isoformat().replace("+00:00", "Z"),
    }
    assert body["items"][0]["customer"] == {
        "display_name": "<script>alert(1)</script>",
        "phone_masked": "***5678",
    }
    serialized = first.text
    assert FULL_PHONE not in serialized
    for forbidden in (
        "phone_number",
        "customer_id",
        "lead_id",
        "city",
        "club",
        "context",
        "messages",
    ):
        assert forbidden not in serialized
    assert body["next_cursor"]
    assert first.headers["cache-control"] == "no-store"

    queue_sql = str(
        database.statements[-1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )
    assert "ORDER BY mbb.conversations.last_message_time DESC" in queue_sql
    assert "mbb.conversations.conversation_id DESC" in queue_sql
    assert "mbb.conversations.status = 'active'" in queue_sql
    assert "mbb.conversations.language_detected = 'french'" in queue_sql
    assert "mbb.conversations.context" not in queue_sql

    database.queue_rows = [
        _queue_row(
            conversation_id=ids[2],
            occurred_at=tied - timedelta(minutes=1),
            content_type="voice_note",
            content="https://provider.example/private-media",
            language="swahili",
        )
    ]
    second = await client.get(
        "/api/v1/operator/conversations",
        params={"limit": 2, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200
    assert [item["conversation_id"] for item in second.json()["items"]] == [
        str(ids[2])
    ]
    assert (
        second.json()["items"][0]["latest_message"]["preview"]
        == "[Ujumbe wa sauti]"
    )
    assert "provider.example" not in second.text
    second_sql = str(database.statements[-1])
    assert "last_message_time" in second_sql
    assert "conversation_id" in second_sql
    assert " < " in second_sql
    assert len({str(value) for value in ids}) == 3

    tampered = body["next_cursor"][:-1] + (
        "A" if body["next_cursor"][-1] != "A" else "B"
    )
    invalid = await client.get(
        "/api/v1/operator/conversations", params={"cursor": tampered}
    )
    assert invalid.status_code == 400
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_queue_validation_empty_latest_and_limit_bound(operator_harness) -> None:
    client, database, _account_value = operator_harness
    now = datetime.now(timezone.utc)
    row = _queue_row(conversation_id=uuid.uuid4(), occurred_at=now)
    row.update(
        latest_content=None,
        latest_content_type=None,
        latest_direction=None,
        latest_occurred_at=None,
    )
    database.queue_rows = [row]
    response = await client.get("/api/v1/operator/conversations")
    assert response.status_code == 200
    assert response.json()["items"][0]["latest_message"] is None
    assert response.json()["items"][0]["awaiting_response_since"] is None

    for params in (
        {"limit": 0},
        {"limit": 51},
        {"status": "not-a-status"},
        {"escalation_state": "closed"},
        {"language": "english"},
    ):
        invalid = await client.get(
            "/api/v1/operator/conversations", params=params
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.asyncio
async def test_detail_is_minimized_and_missing_is_indistinguishable(
    operator_harness,
) -> None:
    client, database, _account_value = operator_harness
    conversation_id = uuid.uuid4()
    database.detail_rows = [
        _detail_row(conversation_id, datetime.now(timezone.utc))
    ]
    response = await client.get(
        f"/api/v1/operator/conversations/{conversation_id}"
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "conversation_id",
        "status",
        "language",
        "message_count",
        "updated_at",
        "customer",
        "lead",
        "open_escalation",
        "ownership",
    }
    assert body["ownership"]["owner_type"] == "ai"
    assert body["ownership"]["human_owner"] is None
    assert body["customer"]["phone_masked"] == "***5678"
    assert len(body["lead"]["product_interests"]) == 5
    assert len(body["lead"]["product_interests"][0]) == 80
    assert FULL_PHONE not in response.text
    for forbidden in (
        "phone_number",
        "customer_id",
        "lead_id",
        "city",
        "club",
        "consent",
        "opt_out",
        "context",
        "messages",
    ):
        assert forbidden not in response.text
    detail_sql = str(
        database.statements[-1].compile(dialect=postgresql.dialect())
    )
    assert "mbb.conversations.context" not in detail_sql

    database.detail_rows = []
    missing = await client.get(
        f"/api/v1/operator/conversations/{conversation_id}"
    )
    inaccessible = await client.get(
        f"/api/v1/operator/conversations/{uuid.uuid4()}"
    )
    assert missing.status_code == inaccessible.status_code == 404
    assert missing.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"
    assert inaccessible.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_message_history_is_bounded_plain_chronological_and_actor_safe(
    operator_harness,
) -> None:
    client, database, _account_value = operator_harness
    conversation_id = uuid.uuid4()
    tied = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    ids = sorted([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()], reverse=True)
    hostile = (
        "<script>alert(1)</script> **markdown** "
        "[link](javascript:alert(1)) https://example.invalid"
    )
    database.message_rows = [
        _message_row(
            message_id=ids[0],
            occurred_at=tied,
            direction="outbound",
            content="legacy outbound",
        ),
        _message_row(
            message_id=ids[1],
            occurred_at=tied,
            direction="inbound",
            content=hostile,
        ),
        _message_row(
            message_id=ids[2],
            occurred_at=tied - timedelta(minutes=1),
            direction="inbound",
            content="https://provider.example/private-media",
            content_type="image",
        ),
    ]

    first = await client.get(
        f"/api/v1/operator/conversations/{conversation_id}/messages",
        params={"limit": 2},
    )
    assert first.status_code == 200
    body = first.json()
    assert [item["message_id"] for item in body["items"]] == [
        str(ids[1]),
        str(ids[0]),
    ]
    assert body["items"][0]["text"] == hostile
    assert body["items"][0]["sender_type"] == "customer"
    assert body["items"][1]["sender_type"] == "unknown"
    assert body["next_older_cursor"]
    assert database.scalar_count == 2
    assert database.execute_count == 1

    database.message_rows = [
        _message_row(
            message_id=ids[2],
            occurred_at=tied - timedelta(minutes=1),
            direction="inbound",
            content="https://provider.example/private-media",
            content_type="image",
        )
    ]
    older = await client.get(
        f"/api/v1/operator/conversations/{conversation_id}/messages",
        params={"limit": 2, "before": body["next_older_cursor"]},
    )
    assert older.status_code == 200
    media_item = older.json()["items"][0]
    assert media_item["text"] is None
    assert media_item["media"] == {"kind": "image", "available": False}
    assert "provider.example" not in older.text
    assert older.json()["next_older_cursor"] is None
    history_sql = str(database.statements[-1])
    assert "ORDER BY mbb.messages.timestamp DESC" in history_sql
    assert "mbb.messages.message_id DESC" in history_sql
    assert " < " in history_sql
    assert "whatsapp_message_id" not in history_sql

    wrong_conversation = await client.get(
        f"/api/v1/operator/conversations/{uuid.uuid4()}/messages",
        params={"before": body["next_older_cursor"]},
    )
    assert wrong_conversation.status_code == 400
    assert wrong_conversation.json()["error"]["code"] == "VALIDATION_ERROR"

    for limit in (0, 51):
        invalid = await client.get(
            f"/api/v1/operator/conversations/{conversation_id}/messages",
            params={"limit": limit},
        )
        assert invalid.status_code == 422


@pytest.mark.asyncio
async def test_empty_and_missing_message_history(operator_harness) -> None:
    client, database, _account_value = operator_harness
    conversation_id = uuid.uuid4()
    database.message_rows = []
    empty = await client.get(
        f"/api/v1/operator/conversations/{conversation_id}/messages"
    )
    assert empty.status_code == 200
    assert empty.json() == {"items": [], "next_older_cursor": None}

    database.conversation_accessible = False
    missing = await client.get(
        f"/api/v1/operator/conversations/{conversation_id}/messages"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "CONVERSATION_NOT_FOUND"


@pytest.mark.asyncio
async def test_typed_service_maintenance_and_internal_errors_do_not_leak(
    operator_harness,
) -> None:
    client, database, _account_value = operator_harness
    database.execute_error = SQLAlchemyError("private database detail")
    unavailable = await client.get("/api/v1/operator/conversations")
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "SERVICE_UNAVAILABLE"
    assert "private database detail" not in unavailable.text
    assert unavailable.json()["error"]["request_id"]

    database.execute_error = RuntimeError("private internal detail")
    internal = await client.get("/api/v1/operator/conversations")
    assert internal.status_code == 500
    assert internal.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "private internal detail" not in internal.text
    assert internal.json()["error"]["request_id"]

    class MaintenanceRedis:
        async def get(self, _key: str) -> bytes:
            return b"1"

    settings = _settings()
    maintenance_db = FakeReadDatabase(_account())
    maintenance_app = _app(settings, object(), maintenance_db)
    maintenance_app.state.redis = MaintenanceRedis()
    maintenance_app.add_middleware(MaintenanceModeMiddleware)
    maintenance_app.add_middleware(RequestTracingMiddleware)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=maintenance_app),
        base_url=ORIGIN,
    ) as maintenance_client:
        maintenance = await maintenance_client.get(
            "/api/v1/operator/conversations",
            headers={"X-Request-ID": "e1-maintenance-request"},
        )
    assert maintenance.status_code == 503
    assert maintenance.json()["error"]["code"] == "MAINTENANCE_MODE"
    assert (
        maintenance.json()["error"]["request_id"] == "e1-maintenance-request"
    )
    assert maintenance.headers["x-request-id"] == "e1-maintenance-request"
    assert maintenance_db.scalar_count == maintenance_db.execute_count == 0


def test_cursor_keyset_handles_ties_and_insert_between_pages() -> None:
    now = datetime.now(timezone.utc)
    account = _account()
    settings = _settings()
    state = BrowserAuthState(redis_client=object(), settings=settings)
    principal = BrowserPrincipal(
        account=account,
        session=BrowserSessionContext(
            raw_token="x",
            record=object(),
            state=state,
        ),
        capabilities=frozenset({"conversation.read"}),
    )
    ids = sorted([uuid.uuid4(), uuid.uuid4(), uuid.uuid4()], reverse=True)
    cursor = operator_conversations._encode_cursor(
        kind="conversation",
        occurred_at=now,
        item_id=ids[1],
        principal=principal,
    )
    boundary = operator_conversations._decode_cursor(
        cursor,
        kind="conversation",
        principal=principal,
    )
    inserted_newer = (now + timedelta(seconds=1), uuid.uuid4())
    all_keys = [
        inserted_newer,
        (now, ids[0]),
        (now, ids[1]),
        (now, ids[2]),
        (now - timedelta(seconds=1), uuid.uuid4()),
    ]
    older = [key for key in all_keys if key < boundary]
    assert (now, ids[0]) not in older
    assert (now, ids[1]) not in older
    assert (now, ids[2]) in older
    assert inserted_newer not in older
