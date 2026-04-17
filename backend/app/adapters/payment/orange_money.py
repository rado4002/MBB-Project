"""
app/adapters/payment/orange_money.py — Orange Money USSD push adapter.

In dev mode (no API key): returns mock success response immediately.
In prod mode: calls the Orange Money REST API, then awaits callback.

Circuit breaker: 3 consecutive failures → open for 60 s.
Retry: up to 3 attempts with exponential back-off (2s, 5s, 10s).
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


class OrangeMoneyError(RuntimeError):
    """Orange Money API unreachable after retries."""


class OrangeMoneyAdapter(BasePaymentAdapter):
    """Orange Money USSD push payment adapter (DRC)."""

    def __init__(self) -> None:
        self._api_key = settings.orange_money_key
        self._base_url = settings.orange_money_base_url
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
            log.error("orange_money.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if not self._circuit_open:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._HALF_OPEN_AFTER_S:
            log.info("orange_money.circuit_half_open", elapsed_s=round(elapsed, 1))
            return
        raise OrangeMoneyError("Orange Money circuit breaker open — try COD instead")

    # ── Dev mock ──────────────────────────────────────────────────────────────

    def _is_dev_mode(self) -> bool:
        return not self._api_key or settings.app_env == "development"

    def _mock_initiate(self, phone: str, amount: float, reference: str) -> dict[str, Any]:
        txn_id = f"OM-MOCK-{uuid.uuid4().hex[:8].upper()}"
        log.info("orange_money.mock.initiated", txn_id=txn_id, phone=phone, amount=amount)
        return {
            "status": "pending",
            "transaction_id": txn_id,
            "payment_url": None,
            "ussd_code": "*144*1*1#",
            "message": f"USSD push sent to {phone}. Dial *144*1*1# to confirm.",
        }

    def _mock_verify(self, transaction_id: str) -> dict[str, Any]:
        log.info("orange_money.mock.verified", transaction_id=transaction_id)
        return {
            "status": "completed",
            "transaction_id": transaction_id,
            "provider_reference": f"OM-REF-{transaction_id[-8:]}",
        }

    # ── Real API calls ────────────────────────────────────────────────────────

    async def _post_with_retry(self, url: str, payload: dict) -> dict[str, Any]:
        """POST to Orange Money API with retry and circuit breaker."""
        self._check_circuit()
        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                    )
                    resp.raise_for_status()
                    self._record_success()
                    return resp.json()
            except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
                last_exc = exc
                self._record_failure()
                log.warning(
                    "orange_money.retry",
                    attempt=attempt + 1,
                    error=str(exc),
                    delay=delay,
                )
                if attempt < len(_RETRY_DELAYS) - 1:
                    await asyncio.sleep(delay)
        raise OrangeMoneyError(f"Orange Money API failed after {_MAX_RETRIES} attempts: {last_exc}")

    # ── BasePaymentAdapter interface ──────────────────────────────────────────

    async def initiate_payment(
        self, phone: str, amount: float, currency: str, reference: str, method: str
    ) -> dict[str, Any]:
        """
        Initiate an Orange Money USSD push payment.

        Args:
            phone:     Customer MSISDN (e.g. "+243 81 234 5678")
            amount:    Amount in CDF
            currency:  "CDF"
            reference: Idempotency key (order UUID)
            method:    "orange_money"

        Returns:
            {"status": "pending", "transaction_id": ..., "ussd_code": ...}
        """
        if self._is_dev_mode():
            return self._mock_initiate(phone, amount, reference)

        log.info("orange_money.initiate", phone=phone, amount=amount, reference=reference)
        payload = {
            "merchant_key": self._api_key,
            "currency": currency,
            "order_id": reference,
            "amount": str(amount),
            "return_url": "",
            "cancel_url": "",
            "notif_url": "",
            "lang": "fr",
            "reference": reference,
            "channel_user_msisdn": phone,
        }
        return await self._post_with_retry(
            f"{self._base_url}/webpayment", payload
        )

    async def verify_payment(self, transaction_id: str) -> dict[str, Any]:
        """
        Query Orange Money API to verify payment status.

        Args:
            transaction_id: Provider-assigned transaction ID

        Returns:
            {"status": "completed" | "pending" | "failed", "transaction_id": ...}
        """
        if self._is_dev_mode():
            return self._mock_verify(transaction_id)

        log.info("orange_money.verify", transaction_id=transaction_id)
        return await self._post_with_retry(
            f"{self._base_url}/transactions/{transaction_id}", {}
        )
