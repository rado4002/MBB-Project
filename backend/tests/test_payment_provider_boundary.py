"""Offline fail-closed evidence while DRC merchant verification is unavailable."""

from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import FastAPI, HTTPException

from app.api.v1.payments import payment_callback, router as payment_router
from app.api.v1.orders import update_order_status as http_order_status
from app.database import get_db
from app.modules.m7_conversion import payment_handler
from app.modules.m7_conversion.payment_handler import process_payment_callback
from app.modules.m7_conversion.service import (
    PaymentPrerequisiteUnavailable,
    initiate_payment,
    process_callback,
    update_order_status,
)
from app.tasks.conversion import (
    _process_payment_callback,
    initiate_payment as initiate_task,
    update_order_status as status_task,
)
from app.schemas.common import OrderStatus
from app.schemas.orders import OrderStatusUpdate


def test_legacy_hmac_helper_fails_closed_without_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(payment_handler.settings, "payment_webhook_secret", "")
    with pytest.raises(payment_handler.HMACVerificationError, match="not configured"):
        payment_handler.verify_payment_hmac(b"{}", "sha256=anything")


@pytest.mark.asyncio
async def test_direct_service_and_http_callbacks_cannot_confirm_or_send() -> None:
    session = AsyncMock()
    order_id = uuid.uuid4()
    for status in ("confirmed", "preparing"):
        with pytest.raises(PaymentPrerequisiteUnavailable):
            await update_order_status(session, order_id=order_id, new_status=status)
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()

    for status in (OrderStatus.confirmed, OrderStatus.preparing):
        with pytest.raises(HTTPException) as exc:
            await http_order_status(order_id, OrderStatusUpdate(status=status), session)
        assert exc.value.status_code == 409
    session.execute.assert_not_awaited()

    with pytest.raises(HTTPException) as exc:
        await payment_callback(None, None)
    assert exc.value.status_code == 503

    with pytest.raises(PaymentPrerequisiteUnavailable):
        await process_payment_callback(session, None)
    session.execute.assert_not_awaited()

    assert (await initiate_payment(order_id=str(order_id), idempotency_key="same"))["status"] == "blocked"
    first = await process_callback(payment_id="provider-ref", provider="airtel", status="success", idempotency_key="same")
    replay = await process_callback(payment_id="provider-ref", provider="airtel", status="success", idempotency_key="same")
    assert first == replay == {"status": "blocked", "reason": "payment_verification_unavailable"}


@pytest.mark.asyncio
async def test_http_callback_returns_503_before_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.tasks.celery_app import celery_app

    monkeypatch.setattr(celery_app, "send_task", lambda *args, **kwargs: pytest.fail("broker dispatch"))
    app = FastAPI()
    app.include_router(payment_router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: None
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post("/api/v1/payments/callback", json={"status": "completed"})
    assert response.status_code == 503
    assert response.json()["detail"] == "payment_provider_verification_unavailable"


@pytest.mark.asyncio
async def test_concurrent_unverified_callbacks_and_status_writes_leave_session_untouched() -> None:
    session = AsyncMock()
    order_id = uuid.uuid4()

    async def blocked_transition(status: str) -> str:
        with pytest.raises(PaymentPrerequisiteUnavailable):
            await update_order_status(session, order_id=order_id, new_status=status)
        return "blocked"

    results = await asyncio.gather(
        blocked_transition("confirmed"), blocked_transition("preparing"),
        process_callback(payment_id="same", provider="airtel", status="success", idempotency_key="one"),
        process_callback(payment_id="same", provider="airtel", status="success", idempotency_key="one"),
    )
    assert results[:2] == ["blocked", "blocked"]
    assert results[2] == results[3] == {"status": "blocked", "reason": "payment_verification_unavailable"}
    session.execute.assert_not_awaited()
    session.commit.assert_not_awaited()


@pytest.mark.asyncio
async def test_celery_callback_rejects_without_notification() -> None:
    result = await _process_payment_callback(
        transaction_id="provider-ref", order_id=str(uuid.uuid4()), provider="airtel",
        status="success", amount_cdf="10000.00", customer_phone="+243900000001",
        timestamp="2026-10-22T00:00:00Z",
    )
    assert result == {"status": "blocked", "reason": "payment_verification_unavailable"}


def test_celery_status_task_rejects_without_retry() -> None:
    assert initiate_task.run(order_id=str(uuid.uuid4()), idempotency_key="same") == {
        "status": "blocked", "reason": "payment_provider_not_configured"
    }
    for status in ("confirmed", "preparing"):
        blocked = status_task.run(order_id=str(uuid.uuid4()), new_status=status, idempotency_key="same")
        assert blocked["status"] == "blocked"
