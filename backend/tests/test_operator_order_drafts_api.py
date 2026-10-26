"""Browser API contract for operator-prepared Order drafts."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import fakeredis.aioredis
import httpx
import pytest

from app.api.v1 import operator_order_drafts
from app.models.order_draft import OrderDraft
from app.modules.m7_conversion.order_drafts import (
    OperatorOrderDraftInsufficientStock,
    OperatorOrderDraftStaleOffer,
    OperatorPreparedOrderDraft,
)
from app.operator_identity.browser_auth import BrowserAuthState, SESSION_COOKIE_NAME
from test_operator_escalation_api import (
    ORIGIN,
    FakeDatabase,
    _account,
    _app,
    _headers,
    _settings,
)


def _body() -> dict[str, object]:
    return {
        "expected_ownership_version": 2,
        "product_id": str(uuid.uuid4()),
        "sellable_item_id": str(uuid.uuid4()),
        "price_id": str(uuid.uuid4()),
        "exchange_rate_id": str(uuid.uuid4()),
        "inventory_updated_at": "2026-10-26T08:00:00Z",
        "quantity": 2,
    }


def _result(body: dict[str, object], account_id: uuid.UUID):
    now = datetime.now(timezone.utc)
    draft = OrderDraft(
        draft_id=uuid.uuid4(), draft_version=1,
        conversation_id=uuid.uuid4(), source_message_id=uuid.uuid4(),
        presented_outbound_message_id=None, created_by_turn_id=None,
        created_by_operator_account_id=account_id,
        operator_operation_key_digest="a" * 64,
        operator_request_fingerprint="b" * 64,
        ownership_version=2, commercial_state_revision=0,
        product_id=uuid.UUID(str(body["product_id"])),
        sellable_item_id=uuid.UUID(str(body["sellable_item_id"])),
        product_name="Air Fryer", model_label="6L", quantity=2,
        price_id=uuid.UUID(str(body["price_id"])),
        unit_price_usd=Decimal("55.00"), price_effective_at=now,
        exchange_rate_id=uuid.UUID(str(body["exchange_rate_id"])),
        unit_price_cdf=Decimal("154000.00"), total_cdf=Decimal("308000.00"),
        exchange_rate_effective_at=now, inventory_status="available",
        inventory_updated_at=now, offer_read_at=now,
        offer_fingerprint="c" * 64, confirmation_code="A1B2C3D4",
        status="awaiting_confirmation",
    )
    return OperatorPreparedOrderDraft(
        draft=draft,
        customer_confirmation_text="Confirmez avec OUI A1B2C3D4.",
        replayed=False,
    )


async def _client(monkeypatch, *, role="operator", failure=None):
    settings = _settings()
    account = _account(role=role)
    database = FakeDatabase(account)
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    state = BrowserAuthState(redis_client=redis, settings=settings)
    session = await state.sessions.create_session(
        account_id=account.account_id,
        auth_version=account.auth_version,
        ip_prefix=state.source_network("127.0.0.1"),
        user_agent="python-httpx/0.27.2",
    )
    calls = []

    async def prepare(_db, **kwargs):
        calls.append(kwargs)
        if failure:
            raise failure
        return _result(kwargs.pop("test_body"), account.account_id)

    body = _body()

    async def prepare_with_body(_db, **kwargs):
        kwargs["test_body"] = body
        return await prepare(_db, **kwargs)

    monkeypatch.setattr(
        operator_order_drafts, "prepare_operator_order_draft", prepare_with_body
    )
    app = _app(settings, redis, database)
    app.include_router(operator_order_drafts.router, prefix="/api/v1")
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 12345)),
        base_url=ORIGIN,
    )
    client.cookies.set(SESSION_COOKIE_NAME, session.token)
    return client, redis, state.csrf_for_session(session.record), calls, body


@pytest.mark.asyncio
async def test_operator_posts_stable_offer_ids_and_receives_unsent_draft(monkeypatch):
    client, redis, csrf, calls, body = await _client(monkeypatch)
    conversation_id = uuid.uuid4()
    key = uuid.uuid4()
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{conversation_id}/order-drafts",
            json=body, headers=_headers(csrf, key),
        )
        assert response.status_code == 201, response.text
        assert response.headers["cache-control"] == "no-store"
        assert response.json()["presented_to_customer"] is False
        assert response.json()["required_customer_reply"] == "OUI A1B2C3D4"
        assert calls[0]["conversation_id"] == conversation_id
        assert calls[0]["idempotency_key"] == key
        assert calls[0]["sellable_item_id"] == uuid.UUID(str(body["sellable_item_id"]))
        assert calls[0]["quantity"] == 2
    finally:
        await client.aclose()
        await redis.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (OperatorOrderDraftStaleOffer(), "PRODUCT_OFFER_STALE"),
        (OperatorOrderDraftInsufficientStock(), "INSUFFICIENT_STOCK"),
    ],
)
async def test_operator_draft_conflicts_have_stable_safe_codes(
    monkeypatch, failure, code
):
    client, redis, csrf, _calls, body = await _client(
        monkeypatch, failure=failure
    )
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/order-drafts",
            json=body, headers=_headers(csrf),
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == code
    finally:
        await client.aclose()
        await redis.aclose()


@pytest.mark.asyncio
async def test_analyst_cannot_prepare_operator_order_draft(monkeypatch):
    client, redis, csrf, calls, body = await _client(monkeypatch, role="analyst")
    try:
        response = await client.post(
            f"/api/v1/operator/conversations/{uuid.uuid4()}/order-drafts",
            json=body, headers=_headers(csrf),
        )
        assert response.status_code == 403
        assert not calls
    finally:
        await client.aclose()
        await redis.aclose()
