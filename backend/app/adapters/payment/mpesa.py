"""
app/adapters/payment/mpesa.py — M-Pesa STK push adapter (DRC/Safaricom).

STK push: customer receives PIN prompt on their phone, enters PIN to confirm.
Circuit breaker + retry pattern identical to Orange/Airtel adapters.
In dev mode (no API key): returns mock success immediately.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import httpx
import structlog

from app.adapters.base import BasePaymentAdapter
from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

_MAX_RETRIES = 3
_RETRY_DELAYS = (2.0, 5.0, 10.0)
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


class MPesaError(RuntimeError):
    """M-Pesa API unreachable after retries."""


class MPesaAdapter(BasePaymentAdapter):
    """M-Pesa STK push payment adapter."""

    def __init__(self) -> None:
        self._api_key = settings.mpesa_key
        self._base_url = settings.mpesa_base_url
        self._failures = 0
        self._circuit_open = False
        self._circuit_opened_at: float = 0.0

    _HALF_OPEN_AFTER_S = 60.0

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _record_success(self) -> None:
        self._failures = 0
        self._circuit_open = False

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _MAX_RETRIES:
            self._circuit_open = True
            self._circuit_opened_at = time.monotonic()
            log.error("mpesa.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if not self._circuit_open:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._HALF_OPEN_AFTER_S:
            log.info("mpesa.circuit_half_open", elapsed_s=round(elapsed, 1))
            return
        raise MPesaError("M-Pesa circuit breaker open — try COD instead")

    # ── Dev mock ──────────────────────────────────────────────────────────────

    def _is_dev_mode(self) -> bool:
        return not self._api_key or settings.app_env == "development"

    def _mock_initiate(self, phone: str, amount: float, reference: str) -> dict[str, Any]:
        txn_id = f"MP-MOCK-{uuid.uuid4().hex[:8].upper()}"
        log.info("mpesa.mock.initiated", txn_id=txn_id, phone=phone, amount=amount)
        return {
            "status": "pending",
            "transaction_id": txn_id,
            "checkout_request_id": txn_id,
            "message": f"STK push sent to {phone}. Enter your M-Pesa PIN to confirm.",
        }

    def _mock_verify(self, transaction_id: str) -> dict[str, Any]:
        log.info("mpesa.mock.verified", transaction_id=transaction_id)
        return {
            "status": "completed",
            "transaction_id": transaction_id,
            "provider_reference": f"MP-REF-{transaction_id[-8:]}",
        }

    # ── Token refresh for Safaricom OAuth ────────────────────────────────────

    async def _get_access_token(self) -> str:
        """Get OAuth2 access token from Safaricom."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{self._base_url}/oauth/v1/generate?grant_type=client_credentials",
                auth=(self._api_key, ""),
            )
            resp.raise_for_status()
            return resp.json()["access_token"]

    async def _post_with_retry(self, url: str, payload: dict) -> dict[str, Any]:
        """POST to M-Pesa API with retry and circuit breaker."""
        self._check_circuit()
        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                token = await self._get_access_token()
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    self._record_success()
                    return resp.json()
            except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
                last_exc = exc
                self._record_failure()
                log.warning("mpesa.retry", attempt=attempt + 1, error=str(exc), delay=delay)
                if attempt < len(_RETRY_DELAYS) - 1:
                    await asyncio.sleep(delay)
        raise MPesaError(f"M-Pesa API failed after {_MAX_RETRIES} attempts: {last_exc}")

    # ── BasePaymentAdapter interface ──────────────────────────────────────────

    async def initiate_payment(
        self, phone: str, amount: float, currency: str, reference: str, method: str
    ) -> dict[str, Any]:
        """
        Initiate an M-Pesa STK push.

        Returns:
            {"status": "pending", "transaction_id": ..., "checkout_request_id": ...}
        """
        if not settings.payment_send_enabled:
            log.warning(
                "mpesa.initiate_skipped_safety_gate",
                reference=reference,
                payment_send_enabled=False,
            )
            return {
                "status": "skipped",
                "reason": "payment_send_disabled",
                "transaction_id": None,
            }

        if self._is_dev_mode():
            return self._mock_initiate(phone, amount, reference)

        log.info("mpesa.initiate", phone=phone, amount=amount, reference=reference)
        payload = {
            "BusinessShortCode": "174379",
            "Password": "",  # Base64(ShortCode + Passkey + Timestamp)
            "Timestamp": "",  # yyyyMMddHHmmss
            "TransactionType": "CustomerPayBillOnline",
            "Amount": int(amount),
            "PartyA": phone,
            "PartyB": "174379",
            "PhoneNumber": phone,
            "CallBackURL": "",
            "AccountReference": reference,
            "TransactionDesc": "MBB ya Kin order payment",
        }
        return await self._post_with_retry(
            f"{self._base_url}/mpesa/stkpush/v1/processrequest", payload
        )

    async def verify_payment(self, transaction_id: str) -> dict[str, Any]:
        """
        Query M-Pesa STK push status.

        Returns:
            {"status": "completed" | "pending" | "failed", "transaction_id": ...}
        """
        if not settings.payment_send_enabled:
            log.warning(
                "mpesa.verify_skipped_safety_gate",
                transaction_id=transaction_id,
                payment_send_enabled=False,
            )
            return {
                "status": "skipped",
                "reason": "payment_send_disabled",
                "transaction_id": transaction_id,
            }

        if self._is_dev_mode():
            return self._mock_verify(transaction_id)

        log.info("mpesa.verify", transaction_id=transaction_id)
        payload = {
            "BusinessShortCode": "174379",
            "Password": "",
            "Timestamp": "",
            "CheckoutRequestID": transaction_id,
        }
        return await self._post_with_retry(
            f"{self._base_url}/mpesa/stkpushquery/v1/query", payload
        )
