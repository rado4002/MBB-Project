from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.browser_auth_deps import get_browser_redis, get_browser_settings
from app.api.browser_auth_errors import (
    BrowserAuthError,
    browser_error_response,
    browser_validation_error_response,
)
from app.api.v1 import auth, operator_accounts
from app.config import Settings
from app.database import get_db
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import OperatorAuditEvent
from app.operator_identity.passwords import hash_password

pytestmark = pytest.mark.skipif(
    not os.environ.get("D1_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable D1 PostgreSQL database",
)

ORIGIN = "https://operator.example"
ADMIN_PASSWORD = "Administrator-Access-42!"
OPERATOR_PASSWORD = "Cobalt-River-83!"


def _settings() -> Settings:
    return Settings(
        browser_auth_enabled=True,
        browser_allowed_origin=ORIGIN,
        browser_session_hmac_secret="s" * 32,
        browser_csrf_hmac_secret="c" * 32,
        browser_session_redis_db=4,
        browser_session_idle_seconds=1800,
        browser_session_absolute_seconds=28800,
        browser_recent_reauth_seconds=600,
        browser_max_sessions_per_account=2,
        browser_session_activity_coalesce_seconds=0,
        operator_audit_retention_days=365,
        operator_security_metadata_retention_days=90,
    )


def _account(username: str, display_name: str, role: str, password: str) -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized=username,
        display_name=display_name,
        email_normalized=f"{username}@example.test",
        password_hash=hash_password(password),
        role=role,
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def harness():
    engine = create_async_engine(os.environ["D1_TEST_DATABASE_URL"])
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as seed_session:
        administrator = _account("admin.user", "Ada Administrator", "administrator", ADMIN_PASSWORD)
        operator = _account("operator.user", "Omar Operator", "operator", OPERATOR_PASSWORD)
        analyst = _account("analyst.user", "Ana Analyst", "analyst", "Analyst-Access-53!")
        seed_session.add_all([administrator, operator, analyst])
        await seed_session.commit()
    administrator_id = administrator.account_id
    operator_id = operator.account_id
    analyst_id = analyst.account_id

    app = FastAPI()
    app.add_exception_handler(BrowserAuthError, browser_error_response)
    app.add_exception_handler(RequestValidationError, browser_validation_error_response)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(operator_accounts.router, prefix="/api/v1")
    app.dependency_overrides[get_browser_settings] = _settings
    app.dependency_overrides[get_browser_redis] = lambda: redis_client

    async def _db_override():
        async with session_factory() as request_session:
            yield request_session

    app.dependency_overrides[get_db] = _db_override
    transport = httpx.ASGITransport(app=app)
    try:
        yield app, transport, session_factory, administrator_id, operator_id, analyst_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "TRUNCATE mbb.operator_audit_security_metadata, "
                    "mbb.operator_audit_events, mbb.operator_accounts CASCADE"
                )
            )
    await redis_client.flushall()
    await redis_client.aclose()
    await engine.dispose()


def _headers(csrf_token: str) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf_token,
        "Origin": ORIGIN,
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }


async def _login(client: httpx.AsyncClient, username: str, password: str) -> httpx.Response:
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    return await client.post(
        "/api/v1/auth/login",
        headers=_headers(csrf),
        json={"username": username, "password": password},
    )


@pytest.mark.asyncio
async def test_administrator_operator_account_lifecycle_is_session_safe(harness) -> None:
    _app, transport, session_factory, _administrator_id, _operator_id, _analyst_id = harness
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as admin_client:
        login = await _login(admin_client, "admin.user", ADMIN_PASSWORD)
        assert login.status_code == 200
        csrf = login.json()["csrf_token"]

        listed = await admin_client.get("/api/v1/operator/accounts")
        assert listed.status_code == 200
        assert [item["username"] for item in listed.json()["items"]] == ["operator.user"]

        initial_password = "Silver-Forest-27!"
        created = await admin_client.post(
            "/api/v1/operator/accounts",
            headers=_headers(csrf),
            json={
                "username": "created.operator",
                "display_name": "Created Operator",
                "email": "created@example.test",
                "password": initial_password,
            },
        )
        assert created.status_code == 201
        assert "password" not in created.text.lower()
        created_id = created.json()["account_id"]
        async with session_factory() as verification_session:
            created_account = await verification_session.get(OperatorAccount, uuid.UUID(created_id))
            assert created_account is not None
            assert created_account.role == "operator"
            assert created_account.must_change_password is False

        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator_client:
            direct_login = await _login(operator_client, "created.operator", initial_password)
            assert direct_login.status_code == 200
            assert direct_login.json()["must_change_password"] is False

            reset_password = "Granite-Cloud-68!"
            reset = await admin_client.post(
                f"/api/v1/operator/accounts/{created_id}/password",
                headers=_headers(csrf),
                json={"new_password": reset_password},
            )
            assert reset.status_code == 200
            async with session_factory() as verification_session:
                assert (await verification_session.get(OperatorAccount, uuid.UUID(created_id))).auth_version == 2
            assert (await operator_client.get("/api/v1/auth/session")).status_code == 401

        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as fresh_client:
            assert (await _login(fresh_client, "created.operator", initial_password)).status_code == 401
            assert (await _login(fresh_client, "created.operator", reset_password)).status_code == 200
            disabled = await admin_client.post(
                f"/api/v1/operator/accounts/{created_id}/disable",
                headers=_headers(csrf),
                json={},
            )
            assert disabled.status_code == 200
            assert disabled.json()["status"] == "disabled"
            async with session_factory() as verification_session:
                assert (await verification_session.get(OperatorAccount, uuid.UUID(created_id))).auth_version == 3
            assert (await fresh_client.get("/api/v1/auth/session")).status_code == 401

        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as disabled_client:
            assert (await _login(disabled_client, "created.operator", reset_password)).status_code == 401

        enabled_password = "Indigo-Harbor-39!"
        enabled = await admin_client.post(
            f"/api/v1/operator/accounts/{created_id}/enable",
            headers=_headers(csrf),
            json={"new_password": enabled_password},
        )
        assert enabled.status_code == 200
        assert enabled.json()["status"] == "active"
        async with session_factory() as verification_session:
            enabled_account = await verification_session.get(OperatorAccount, uuid.UUID(created_id))
            assert enabled_account.auth_version == 4
            assert enabled_account.must_change_password is False
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as enabled_client:
            assert (await _login(enabled_client, "created.operator", enabled_password)).status_code == 200

        async with session_factory() as verification_session:
            audits = (
                await verification_session.scalars(
                    select(OperatorAuditEvent).where(
                        OperatorAuditEvent.target_id == created_id,
                        OperatorAuditEvent.action.like("operator_account.%"),
                    )
                )
            ).all()
        assert [event.action for event in audits] == [
            "operator_account.provisioned",
            "operator_account.password_reset",
            "operator_account.disabled",
            "operator_account.reactivated_with_password_reset",
        ]
        audit_text = " ".join(str(event.event_metadata) for event in audits)
        for secret in (initial_password, reset_password, enabled_password):
            assert secret not in audit_text


@pytest.mark.asyncio
async def test_account_api_enforces_capability_targets_and_safe_conflicts(harness) -> None:
    _app, transport, _session_factory, administrator_id, _operator_id, analyst_id = harness
    for username, password in (
        ("operator.user", OPERATOR_PASSWORD),
        ("analyst.user", "Analyst-Access-53!"),
    ):
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
            login = await _login(client, username, password)
            assert login.status_code == 200
            assert (await client.get("/api/v1/operator/accounts")).status_code == 403

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as admin_client:
        login = await _login(admin_client, "admin.user", ADMIN_PASSWORD)
        csrf = login.json()["csrf_token"]
        forbidden_admin_target = await admin_client.post(
            f"/api/v1/operator/accounts/{administrator_id}/disable",
            headers=_headers(csrf),
            json={},
        )
        assert forbidden_admin_target.status_code == 403
        forbidden_analyst_target = await admin_client.post(
            f"/api/v1/operator/accounts/{analyst_id}/password",
            headers=_headers(csrf),
            json={"new_password": "Quartz-Meadow-37!"},
        )
        assert forbidden_analyst_target.status_code == 403

        duplicate = await admin_client.post(
            "/api/v1/operator/accounts",
            headers=_headers(csrf),
            json={
                "username": "operator.user",
                "display_name": "Duplicate Operator",
                "email": "different@example.test",
                "password": "Copper-Orchard-48!",
            },
        )
        assert duplicate.status_code == 409
        assert duplicate.json()["error"]["code"] == "OPERATOR_ACCOUNT_CONFLICT"
        assert "integrity" not in duplicate.text.lower()


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_operator_state(harness, monkeypatch) -> None:
    _app, transport, session_factory, _administrator_id, operator_id, _analyst_id = harness
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as admin_client:
        login = await _login(admin_client, "admin.user", ADMIN_PASSWORD)
        csrf = login.json()["csrf_token"]
        monkeypatch.setattr(
            "app.operator_identity.accounts.append_operator_audit_event",
            AsyncMock(side_effect=SQLAlchemyError("fictional audit failure")),
        )
        response = await admin_client.post(
            f"/api/v1/operator/accounts/{operator_id}/disable",
            headers=_headers(csrf),
            json={},
        )
        assert response.status_code == 503
        assert "fictional" not in response.text
    async with session_factory() as verification_session:
        account = await verification_session.get(OperatorAccount, operator_id)
        assert account is not None
        assert account.status == "active"
        assert account.auth_version == 1
