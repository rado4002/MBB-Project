"""
app/adapters/payment/airtel_money.py — Airtel Money USSD push adapter (DRC).

Circuit breaker + retry pattern identical to OrangeMoneyAdapter.
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


class AirtelMoneyError(RuntimeError):
    """Airtel Money API unreachable after retries."""


class AirtelMoneyAdapter(BasePaymentAdapter):
    """Airtel Money USSD push payment adapter (DRC)."""

    def __init__(self) -> None:
        self._api_key = settings.airtel_money_key
        self._base_url = settings.airtel_money_base_url
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
            log.error("airtel_money.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if not self._circuit_open:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._HALF_OPEN_AFTER_S:
            log.info("airtel_money.circuit_half_open", elapsed_s=round(elapsed, 1))
            return
        raise AirtelMoneyError("Airtel Money circuit breaker open — try COD instead")

    # ── Dev mock ──────────────────────────────────────────────────────────────

    def _is_dev_mode(self) -> bool:
        return not self._api_key or settings.app_env == "development"

    def _mock_initiate(self, phone: str, amount: float, reference: str) -> dict[str, Any]:
        txn_id = f"AM-MOCK-{uuid.uuid4().hex[:8].upper()}"
        log.info("airtel_money.mock.initiated", txn_id=txn_id, phone=phone, amount=amount)
        return {
            "status": "pending",
            "transaction_id": txn_id,
            "ussd_code": "*185#",
            "message": f"USSD push sent to {phone}. Dial *185# to confirm.",
        }

    def _mock_verify(self, transaction_id: str) -> dict[str, Any]:
        log.info("airtel_money.mock.verified", transaction_id=transaction_id)
        return {
            "status": "completed",
            "transaction_id": transaction_id,
            "provider_reference": f"AM-REF-{transaction_id[-8:]}",
        }

    # ── Real API calls ────────────────────────────────────────────────────────

    async def _request_with_retry(
        self, method: str, url: str, payload: dict | None = None
    ) -> dict[str, Any]:
        """Make HTTP request to Airtel Money API with retry and circuit breaker."""
        self._check_circuit()
        last_exc: Exception | None = None
        for attempt, delay in enumerate(_RETRY_DELAYS):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.request(
                        method,
                        url,
                        json=payload,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                            "X-Country": "CD",
                            "X-Currency": "CDF",
                        },
                    )
                    resp.raise_for_status()
                    self._record_success()
                    return resp.json()
            except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001
                last_exc = exc
                self._record_failure()
                log.warning(
                    "airtel_money.retry",
                    attempt=attempt + 1,
                    error=str(exc),
                    delay=delay,
                )
                if attempt < len(_RETRY_DELAYS) - 1:
                    await asyncio.sleep(delay)
        raise AirtelMoneyError(f"Airtel Money API failed after {_MAX_RETRIES} attempts: {last_exc}")

    # ── BasePaymentAdapter interface ──────────────────────────────────────────

    async def initiate_payment(
        self, phone: str, amount: float, currency: str, reference: str, method: str
    ) -> dict[str, Any]:
        """
        Initiate an Airtel Money USSD push payment.

        Returns:
            {"status": "pending", "transaction_id": ..., "ussd_code": ...}
        """
        if not settings.payment_send_enabled:
            log.warning(
                "airtel_money.initiate_skipped_safety_gate",
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

        log.info("airtel_money.initiate", phone=phone, amount=amount, reference=reference)
        payload = {
            "reference": reference,
            "subscriber": {"country": "CD", "currency": currency, "msisdn": phone},
            "transaction": {"amount": str(amount), "country": "CD", "currency": currency, "id": reference},
        }
        return await self._request_with_retry(
            "POST", f"{self._base_url}/merchant/v1/payments/", payload
        )

    async def verify_payment(self, transaction_id: str) -> dict[str, Any]:
        """
        Query Airtel Money API for transaction status.

        Returns:
            {"status": "completed" | "pending" | "failed", "transaction_id": ...}
        """
        if not settings.payment_send_enabled:
            log.warning(
                "airtel_money.verify_skipped_safety_gate",
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

        log.info("airtel_money.verify", transaction_id=transaction_id)
        return await self._request_with_retry(
            "GET", f"{self._base_url}/standard/v1/payments/{transaction_id}"
        )
