from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.browser_auth_deps import get_browser_redis, get_browser_settings
from app.api.browser_auth_errors import (
    BrowserAuthError,
    browser_error_response,
    browser_validation_error_response,
)
from app.api.v1 import auth, commerce_admin
from app.config import Settings
from app.database import get_db
from app.models.catalog import Product, ProductMedia
from app.models.operator_audit import OperatorAuditEvent
from app.models.operator_account import OperatorAccount
from app.operator_identity.browser_auth import SESSION_COOKIE_NAME
from app.operator_identity.passwords import hash_password

DATABASE_URL = os.environ.get("AI2B_TEST_DATABASE_URL")
ORIGIN = "https://operator.example"
ADMIN_PASSWORD = "Administrator-Access-42!"
OPERATOR_PASSWORD = "Operator-Access-42!"

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI2B_TEST_DATABASE_URL is required for commerce browser API evidence",
)


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


def _account(username: str, role: str, password: str) -> OperatorAccount:
    now = datetime.now(timezone.utc)
    return OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized=username,
        display_name=username.title(),
        email_normalized=None,
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
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL)
    redis_client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    truncate = text(
        """
        TRUNCATE TABLE
            mbb.inventory_statuses,
            mbb.exchange_rates,
            mbb.sellable_item_prices,
            mbb.sellable_items,
            mbb.products,
            mbb.operator_audit_security_metadata,
            mbb.operator_audit_events,
            mbb.operator_accounts
        RESTART IDENTITY CASCADE
        """
    )
    async with engine.begin() as connection:
        await connection.execute(truncate)
    administrator = _account("commerce.admin", "administrator", ADMIN_PASSWORD)
    operator = _account("commerce.operator", "operator", OPERATOR_PASSWORD)
    async with factory() as session:
        session.add_all([administrator, operator])
        await session.commit()

    app = FastAPI()
    app.add_exception_handler(BrowserAuthError, browser_error_response)
    app.add_exception_handler(RequestValidationError, browser_validation_error_response)
    app.include_router(auth.router, prefix="/api/v1")
    app.include_router(commerce_admin.router, prefix="/api/v1")
    app.dependency_overrides[get_browser_settings] = _settings
    app.dependency_overrides[get_browser_redis] = lambda: redis_client

    async def _db_override():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = _db_override
    transport = httpx.ASGITransport(app=app)
    try:
        yield transport, factory, administrator.account_id
    finally:
        async with engine.begin() as connection:
            await connection.execute(truncate)
        await redis_client.flushall()
        await redis_client.aclose()
        await engine.dispose()


def _headers(csrf: str, *, origin: str = ORIGIN) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf,
        "Origin": origin,
        "Sec-Fetch-Site": "same-origin",
        "Content-Type": "application/json",
    }


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    csrf = (await client.get("/api/v1/auth/csrf")).json()["csrf_token"]
    response = await client.post(
        "/api/v1/auth/login",
        headers=_headers(csrf),
        json={"username": username, "password": password},
    )
    assert response.status_code == 200
    return response.json()["csrf_token"]


@pytest.mark.asyncio
async def test_administrator_allowed_operator_and_unauthenticated_denied(harness) -> None:
    transport, factory, _administrator_id = harness
    product_body = {
        "name": "Fictional Air Fryer",
        "category_code": "air_fryer",
        "description": "Fictional API fixture.",
    }
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as anonymous:
        assert (await anonymous.get("/api/v1/operator/commerce/products")).status_code == 401
        anonymous.cookies.set(SESSION_COOKIE_NAME, "invalid-session-token")
        assert (await anonymous.get("/api/v1/operator/commerce/products")).status_code == 401

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator:
        operator_csrf = await _login(operator, "commerce.operator", OPERATOR_PASSWORD)
        denied = await operator.post(
            "/api/v1/operator/commerce/products",
            headers=_headers(operator_csrf),
            json={**product_body, "name": "Denied Product"},
        )
        assert denied.status_code == 403

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as administrator:
        admin_csrf = await _login(administrator, "commerce.admin", ADMIN_PASSWORD)
        created = await administrator.post(
            "/api/v1/operator/commerce/products",
            headers=_headers(admin_csrf),
            json=product_body,
        )
        assert created.status_code == 201
        assert created.json()["name"] == "Fictional Air Fryer"
        listed = await administrator.get("/api/v1/operator/commerce/products")
        assert listed.status_code == 200
        assert len(listed.json()["items"]) == 1
        assert listed.headers["cache-control"] == "no-store"

    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(
            select(func.count()).select_from(Product).where(Product.name == "Denied Product")
        ) == 0


@pytest.mark.asyncio
async def test_commerce_writes_preserve_csrf_origin_and_strict_payload_guards(harness) -> None:
    transport, _factory, _administrator_id = harness
    body = {
        "name": "Fictional Air Fryer",
        "category_code": "air_fryer",
        "description": "Fictional API fixture.",
    }
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        no_csrf = await client.post(
            "/api/v1/operator/commerce/products",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json=body,
        )
        assert no_csrf.status_code == 403
        bad_origin = await client.post(
            "/api/v1/operator/commerce/products",
            headers=_headers(csrf, origin="https://attacker.example"),
            json=body,
        )
        assert bad_origin.status_code == 403
        unknown = await client.post(
            "/api/v1/operator/commerce/products",
            headers=_headers(csrf),
            json={**body, "current_price": "60.00"},
        )
        assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_disabled_browser_account_session_fails_closed(harness) -> None:
    transport, factory, administrator_id = harness
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        await _login(client, "commerce.admin", ADMIN_PASSWORD)
        async with factory() as session:
            account = await session.get(OperatorAccount, administrator_id)
            assert account is not None
            account.status = "disabled"
            account.auth_version += 1
            await session.commit()
        response = await client.get("/api/v1/operator/commerce/products")
        assert response.status_code == 401


@pytest.mark.asyncio
async def test_product_media_maintenance_is_administrator_only_and_audited(
    harness,
) -> None:
    transport, factory, _administrator_id = harness
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as administrator:
        csrf = await _login(administrator, "commerce.admin", ADMIN_PASSWORD)
        product = await administrator.post(
            "/api/v1/operator/commerce/products",
            headers=_headers(csrf),
            json={
                "name": "Fictional Media Fryer",
                "category_code": "air_fryer",
                "description": "Fictional API media fixture.",
            },
        )
        assert product.status_code == 201
        product_id = product.json()["product_id"]
        created = await administrator.post(
            "/api/v1/operator/commerce/product-media",
            headers=_headers(csrf),
            json={
                "product_id": product_id,
                "asset_url": "https://example.invalid/product/admin.jpg",
                "alt_text": "Fictional product front view",
                "is_primary": True,
                "display_order": 1,
            },
        )
        assert created.status_code == 201
        assert created.json()["is_primary"] is True
        media_id = created.json()["media_id"]
        listed = await administrator.get(
            f"/api/v1/operator/commerce/products/{product_id}/media"
        )
        assert listed.status_code == 200
        assert [item["media_id"] for item in listed.json()["items"]] == [media_id]
        updated = await administrator.patch(
            f"/api/v1/operator/commerce/product-media/{media_id}",
            headers=_headers(csrf),
            json={"alt_text": "Updated fictional product front view"},
        )
        assert updated.status_code == 200
        assert updated.json()["alt_text"] == "Updated fictional product front view"
        primary = await administrator.put(
            f"/api/v1/operator/commerce/product-media/{media_id}/primary",
            headers=_headers(csrf),
            json={},
        )
        assert primary.status_code == 200

    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as operator:
        operator_csrf = await _login(operator, "commerce.operator", OPERATOR_PASSWORD)
        denied = await operator.patch(
            f"/api/v1/operator/commerce/product-media/{media_id}",
            headers=_headers(operator_csrf),
            json={"active": False},
        )
        assert denied.status_code == 403

    async with factory() as session:
        media = await session.get(ProductMedia, uuid.UUID(media_id))
        assert media is not None and media.active is True
        actions = list(
            (
                await session.scalars(
                    select(OperatorAuditEvent.action).where(
                        OperatorAuditEvent.target_type == "product_media",
                        OperatorAuditEvent.target_id == media_id,
                    )
                )
            ).all()
        )
        assert actions == [
            "commerce.product_media.created",
            "commerce.product_media.updated",
            "commerce.product_media.primary_changed",
        ]


@pytest.mark.asyncio
async def test_product_media_writes_keep_browser_and_payload_guards(harness) -> None:
    transport, _factory, _administrator_id = harness
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN) as client:
        csrf = await _login(client, "commerce.admin", ADMIN_PASSWORD)
        body = {
            "product_id": str(uuid.uuid4()),
            "asset_url": "https://example.invalid/product/admin.jpg",
        }
        no_csrf = await client.post(
            "/api/v1/operator/commerce/product-media",
            headers={"Origin": ORIGIN, "Content-Type": "application/json"},
            json=body,
        )
        assert no_csrf.status_code == 403
        bad_origin = await client.post(
            "/api/v1/operator/commerce/product-media",
            headers=_headers(csrf, origin="https://attacker.example"),
            json=body,
        )
        assert bad_origin.status_code == 403
        unsafe_url = await client.post(
            "/api/v1/operator/commerce/product-media",
            headers=_headers(csrf),
            json={**body, "asset_url": "http://example.invalid/product/admin.jpg"},
        )
        assert unsafe_url.status_code == 422
        unknown_field = await client.post(
            "/api/v1/operator/commerce/product-media",
            headers=_headers(csrf),
            json={**body, "storage_bucket": "not-authorized"},
        )
        assert unknown_field.status_code == 422
