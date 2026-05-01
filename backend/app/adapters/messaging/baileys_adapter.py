"""
Baileys messaging adapter.

Sends outbound messages to the Baileys Node.js bridge (dev/testing only).
The bridge maintains the WhatsApp Web session and translates HTTP calls
into WA socket events.

Circuit breaker: after 3 consecutive failures the adapter raises
MessagingAdapterError to prevent cascading failures (DRC connectivity drops).
"""
from __future__ import annotations

import asyncio
import time

import httpx
import structlog

from app.adapters.base import BaseMessagingAdapter
from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
_MAX_RETRIES = 3
_RETRY_DELAYS = (2.0, 5.0, 10.0)   # exponential back-off steps


class MessagingAdapterError(RuntimeError):
    """Raised when the messaging backend is unreachable after retries."""


class BaileysAdapter(BaseMessagingAdapter):
    """HTTP adapter for the Baileys WhatsApp bridge container."""

    def __init__(self) -> None:
        self._base_url = settings.baileys_url  # e.g. http://baileys:3000
        self._failures = 0
        self._circuit_open = False
        self._circuit_opened_at: float = 0.0

    # ── Circuit breaker helpers ────────────────────────────────────────────────

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
            log.error("baileys.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if not self._circuit_open:
            return
        elapsed = time.monotonic() - self._circuit_opened_at
        if elapsed >= self._HALF_OPEN_AFTER_S:
            log.info("baileys.circuit_half_open", elapsed_s=round(elapsed, 1))
            return
        raise MessagingAdapterError("Baileys circuit breaker open — too many failures")

    # ── BaseMessagingAdapter interface ────────────────────────────────────────

    async def send_message(self, phone: str, text: str) -> str:
        """
        POST /send/text to Baileys bridge.
        Returns the bridge-assigned message ID.
        """
        self._check_circuit()
        payload = {"phone": phone, "message": text}

        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(f"{self._base_url}/send", json=payload)
                    resp.raise_for_status()
                    data = resp.json()
                    self._record_success()
                    msg_id: str = data.get("id", "")
                    log.info("baileys.sent", phone=phone, msg_id=msg_id)
                    return msg_id
            except (httpx.HTTPError, Exception) as exc:
                log.warning("baileys.send_failed", attempt=attempt, error=str(exc))
                self._record_failure()
                if delay is None:
                    raise MessagingAdapterError(f"Baileys unreachable after {attempt} attempts") from exc
                await asyncio.sleep(delay)

        return ""  # unreachable

    async def send_template(self, phone: str, template_name: str, params: list[str]) -> str:
        """
        Baileys doesn't support official templates — send as formatted text.
        Template parameters are injected in order.
        """
        text = template_name
        for i, p in enumerate(params, start=1):
            text = text.replace(f"{{{{{i}}}}}", p)
        return await self.send_message(phone, text)
