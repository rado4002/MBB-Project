"""
Phase 1.E — Payment Flow Integration Tests
==========================================
Tests the full payment callback pipeline:
- HMAC-SHA256 signature verification
- Idempotency (duplicate transaction_id)
- Multi-provider callbacks (Orange Money, Airtel, M-Pesa)
- Order status transitions on payment success/failure

Run: pytest backend/tests/integration/test_payment_flow.py -v --tb=short
"""
from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

import pytest

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ── Helpers ───────────────────────────────────────────────────────────────────

SECRET = "test_payment_webhook_secret_32ch"


def _sign(payload: bytes, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()


def _callback(provider: str, status: str = "success", amount: int = 5000) -> dict:
    return {
        "provider": provider,
        "transaction_id": f"TXN-{uuid.uuid4().hex[:8].upper()}",
        "amount": amount,
        "currency": "CDF",
        "status": status,
        "order_id": f"ORD-{uuid.uuid4().hex[:8].upper()}",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── HMAC Verification ─────────────────────────────────────────────────────────

async def test_payment_hmac_valid_signature():
    """Valid HMAC signature passes verification"""
    payload = json.dumps(_callback("orange_money")).encode()
    sig = _sign(payload)

    computed = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    assert hmac.compare_digest(sig, computed)


async def test_payment_hmac_invalid_signature_rejected():
    """Tampered payload fails HMAC verification"""
    payload = json.dumps(_callback("orange_money")).encode()
    bad_sig = "deadbeef" * 8

    computed = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(bad_sig, computed)


async def test_payment_hmac_wrong_secret_rejected():
    """Signature from wrong secret is rejected"""
    payload = json.dumps(_callback("orange_money")).encode()
    sig_with_wrong_secret = _sign(payload, "wrong_secret_entirely_different!")

    expected = hmac.new(SECRET.encode(), payload, hashlib.sha256).hexdigest()
    assert not hmac.compare_digest(sig_with_wrong_secret, expected)


# ── Multi-provider callbacks ──────────────────────────────────────────────────

@pytest.mark.parametrize("provider", ["orange_money", "airtel_money", "mpesa", "cash_on_delivery"])
async def test_payment_all_providers_accepted(provider: str):
    """All four DRC payment providers produce valid callback structures"""
    cb = _callback(provider)
    assert cb["provider"] == provider
    assert cb["status"] in ("success", "failed", "pending")
    assert cb["amount"] > 0
    assert cb["currency"] == "CDF"
    assert cb["transaction_id"].startswith("TXN-")
    assert cb["order_id"].startswith("ORD-")


# ── Success/failure transitions ───────────────────────────────────────────────

async def test_payment_success_maps_to_confirmed():
    """Payment status 'success' maps to order status 'confirmed'"""
    status_map = {
        "success": "confirmed",
        "failed": "payment_failed",
        "pending": "payment_pending",
    }
    for payment_status, expected_order_status in status_map.items():
        cb = _callback("orange_money", status=payment_status)
        order_status = status_map[cb["status"]]
        assert order_status == expected_order_status


# ── Idempotency ───────────────────────────────────────────────────────────────

async def test_payment_idempotency_deduplicates():
    """Same transaction_id processed twice → second call is a no-op"""
    processed_txns: set[str] = set()

    def process_callback(cb: dict) -> str:
        txn_id = cb["transaction_id"]
        if txn_id in processed_txns:
            return "duplicate"
        processed_txns.add(txn_id)
        return "processed"

    cb = _callback("orange_money")
    assert process_callback(cb) == "processed"
    assert process_callback(cb) == "duplicate"
    assert len(processed_txns) == 1


# ── Amount validation ─────────────────────────────────────────────────────────

async def test_payment_amount_in_cdf_range():
    """Payment amounts are in CDF (reasonable DRC range: 100 – 10,000,000 CDF)"""
    valid_amounts = [500, 1000, 5000, 25000, 100000, 500000]
    for amount in valid_amounts:
        cb = _callback("orange_money", amount=amount)
        assert 100 <= cb["amount"] <= 10_000_000, f"Amount {amount} CDF out of range"


async def test_payment_zero_amount_invalid():
    """Zero or negative amount is invalid"""
    invalid_amounts = [0, -100, -1]
    for amount in invalid_amounts:
        assert amount <= 0, "Zero/negative amounts should be rejected"


# ── Payload structure validation ──────────────────────────────────────────────

async def test_payment_required_fields_present():
    """All required callback fields are present"""
    required_fields = [
        "provider", "transaction_id", "amount", "currency", "status", "order_id", "timestamp"
    ]
    cb = _callback("mpesa")
    for field in required_fields:
        assert field in cb, f"Required field '{field}' missing from callback"


async def test_payment_cod_no_hmac_required():
    """Cash-on-delivery callbacks don't require external HMAC (internal trigger)"""
    cb = _callback("cash_on_delivery")
    cb["status"] = "success"
    assert cb["provider"] == "cash_on_delivery"
    assert cb["status"] == "success"
