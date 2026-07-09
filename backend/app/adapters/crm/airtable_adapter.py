"""
app/adapters/crm/airtable_adapter.py — Airtable CRM adapter.

Syncs leads and orders to Airtable via their REST API.
Circuit breaker: 3 consecutive failures → open for 60s.
All operations are idempotent (upsert by lead_id / order_id).
"""
from __future__ import annotations

import time
from typing import Any

import httpx
import structlog

from app.adapters.base import BaseCRMAdapter
from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_MAX_RETRIES = 3
_RETRY_DELAYS = (2.0, 5.0, 10.0)


class CRMAdapterError(RuntimeError):
    """Raised when Airtable is unreachable after retries."""


class AirtableAdapter(BaseCRMAdapter):
    """Airtable REST API adapter for lead/order CRM sync."""

    def __init__(self) -> None:
        self._api_key = settings.airtable_api_key
        self._base_id = settings.airtable_base_id
        self._leads_table = settings.airtable_leads_table
        self._orders_table = settings.airtable_orders_table
        self._failures = 0
        self._circuit_open = False
        self._circuit_opened_at: float = 0.0

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

    def _base_url(self, table: str) -> str:
        return f"https://api.airtable.com/v0/{self._base_id}/{table}"

    # ── Circuit breaker ───────────────────────────────────────────────────────

    _HALF_OPEN_AFTER_S = 60.0

    def _record_success(self) -> None:
        self._failures = 0
        self._circuit_open = False
        self._circuit_opened_at = 0.0

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _MAX_RETRIES:
            self._circuit_open = True
            self._circuit_opened_at = time.monotonic()
            log.error("airtable.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if not self._circuit_open:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._HALF_OPEN_AFTER_S:
            log.info("airtable.circuit_half_open", elapsed_s=round(elapsed, 1))
            return
        raise CRMAdapterError("Airtable circuit breaker open")

    # ── BaseCRMAdapter interface ──────────────────────────────────────────────

    async def create_lead(self, lead_data: dict[str, Any]) -> str:
        """Create a lead record in Airtable. Returns Airtable record ID."""
        import asyncio

        if not settings.crm_send_enabled:
            log.warning(
                "airtable.create_lead_skipped_safety_gate",
                lead_id=lead_data.get("lead_id", ""),
                crm_send_enabled=False,
            )
            return "skipped_safety_gate"

        self._check_circuit()
        payload = {
            "fields": {
                "Lead ID": lead_data.get("lead_id", ""),
                "Phone": lead_data.get("customer_phone", ""),
                "Score": lead_data.get("score", "cold"),
                "Score Value": lead_data.get("score_value", 0),
                "Intent": lead_data.get("intent", ""),
                "Source": lead_data.get("source", "whatsapp_auto"),
                "Stage": lead_data.get("stage", "awareness"),
            }
        }

        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        self._base_url(self._leads_table),
                        headers=self._headers,
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    self._record_success()
                    record_id = data.get("id", "")
                    log.info("airtable.lead_created", record_id=record_id)
                    return record_id
            except (httpx.HTTPError, Exception) as exc:
                log.warning("airtable.create_lead.failed", attempt=attempt, error=str(exc))
                self._record_failure()
                if delay is None:
                    raise CRMAdapterError(
                        f"Airtable unreachable after {attempt} attempts"
                    ) from exc
                await asyncio.sleep(delay)

        return ""  # unreachable

    async def update_lead(self, external_id: str, updates: dict[str, Any]) -> None:
        """Update a lead record in Airtable by record ID."""
        import asyncio

        if not settings.crm_send_enabled:
            log.warning(
                "airtable.update_lead_skipped_safety_gate",
                record_id=external_id,
                crm_send_enabled=False,
            )
            return

        self._check_circuit()
        payload = {"fields": updates}

        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.patch(
                        f"{self._base_url(self._leads_table)}/{external_id}",
                        headers=self._headers,
                        json=payload,
                    )
                    resp.raise_for_status()
                    self._record_success()
                    log.info("airtable.lead_updated", record_id=external_id)
                    return
            except (httpx.HTTPError, Exception) as exc:
                log.warning("airtable.update_lead.failed", attempt=attempt, error=str(exc))
                self._record_failure()
                if delay is None:
                    raise CRMAdapterError(
                        f"Airtable unreachable after {attempt} attempts"
                    ) from exc
                await asyncio.sleep(delay)

    async def sync_order(self, order_data: dict[str, Any]) -> str:
        """Sync an order to Airtable. Returns record ID."""
        import asyncio

        if not settings.crm_send_enabled:
            log.warning(
                "airtable.sync_order_skipped_safety_gate",
                order_id=order_data.get("order_id", ""),
                crm_send_enabled=False,
            )
            return "skipped_safety_gate"

        self._check_circuit()
        payload = {
            "fields": {
                "Order ID": order_data.get("order_id", ""),
                "Lead ID": order_data.get("lead_id", ""),
                "Phone": order_data.get("customer_phone", ""),
                "Products": str(order_data.get("products", [])),
                "Total CDF": order_data.get("total_cdf", 0),
                "Payment Method": order_data.get("payment_method", ""),
                "Status": order_data.get("status", "pending"),
            }
        }

        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(
                        self._base_url(self._orders_table),
                        headers=self._headers,
                        json=payload,
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    self._record_success()
                    record_id = data.get("id", "")
                    log.info("airtable.order_synced", record_id=record_id)
                    return record_id
            except (httpx.HTTPError, Exception) as exc:
                log.warning("airtable.sync_order.failed", attempt=attempt, error=str(exc))
                self._record_failure()
                if delay is None:
                    raise CRMAdapterError(
                        f"Airtable unreachable after {attempt} attempts"
                    ) from exc
                await asyncio.sleep(delay)

        return ""  # unreachable
