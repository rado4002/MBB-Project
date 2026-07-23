"""
WhatsApp Official (Business API) messaging adapter.

Sends outbound messages via the WhatsApp Cloud API (Meta).
Used in production (`WHATSAPP_MODE=official`).

Circuit breaker: after 3 consecutive failures raises MessagingAdapterError.
Message templates must be pre-approved by Meta before use.
"""
from __future__ import annotations

import asyncio

import httpx
import structlog

from app.adapters.base import BaseMessagingAdapter
from app.config import get_settings

log = structlog.get_logger(__name__)
settings = get_settings()

_GRAPH_API = "https://graph.facebook.com/v19.0"
_TIMEOUT = httpx.Timeout(15.0, connect=5.0)
_MAX_RETRIES = 3
_RETRY_DELAYS = (2.0, 5.0, 15.0)


def _mask_phone(phone: str) -> str:
    if len(phone) <= 7:
        return "***"
    return f"{phone[:4]}***{phone[-3:]}"


class MessagingAdapterError(RuntimeError):
    """Raised when the WA Cloud API is unreachable after retries."""


class WhatsAppOfficialAdapter(BaseMessagingAdapter):
    """Calls the Meta WhatsApp Cloud API to send messages."""

    def __init__(self) -> None:
        self._phone_number_id = settings.whatsapp_phone_number_id
        self._token = settings.whatsapp_api_token
        self._failures = 0
        self._circuit_open = False

    # ── Circuit breaker ───────────────────────────────────────────────────────

    def _record_success(self) -> None:
        self._failures = 0
        self._circuit_open = False

    def _record_failure(self) -> None:
        self._failures += 1
        if self._failures >= _MAX_RETRIES:
            self._circuit_open = True
            log.error("wa_official.circuit_open", failures=self._failures)

    def _check_circuit(self) -> None:
        if self._circuit_open:
            raise MessagingAdapterError("WhatsApp Official circuit breaker open")

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }

    # ── BaseMessagingAdapter interface ────────────────────────────────────────

    async def send_message(
        self,
        phone: str,
        text: str,
        *,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Send a free-form text message via the WhatsApp Cloud API.
        Returns the WA message ID.
        """
        del idempotency_key
        if not settings.whatsapp_send_enabled:
            log.warning(
                "wa_official.send_skipped_safety_gate",
                phone=_mask_phone(phone),
                chars=len(text),
                whatsapp_send_enabled=False,
            )
            return ""

        self._check_circuit()
        # Cloud API requires phone without '+' and must be in E.164
        to = phone.lstrip("+")
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "text",
            "text": {"preview_url": False, "body": text},
        }
        url = f"{_GRAPH_API}/{self._phone_number_id}/messages"

        for attempt, delay in enumerate((*_RETRY_DELAYS, None), start=1):
            try:
                async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                    resp = await client.post(url, json=payload, headers=self._headers())
                    resp.raise_for_status()
                    data = resp.json()
                    self._record_success()
                    msg_id: str = (data.get("messages") or [{}])[0].get("id", "")
                    log.info("wa_official.sent", phone=phone, msg_id=msg_id)
                    return msg_id
            except (httpx.HTTPError, Exception) as exc:
                log.warning("wa_official.send_failed", attempt=attempt, error=str(exc))
                self._record_failure()
                if delay is None:
                    raise MessagingAdapterError(
                        f"WhatsApp Official unreachable after {attempt} attempts"
                    ) from exc
                await asyncio.sleep(delay)

        return ""  # unreachable

    async def send_template(self, phone: str, template_name: str, params: list[str]) -> str:
        """
        Send a pre-approved Meta template message with positional parameters.
        """
        if not settings.whatsapp_send_enabled:
            log.warning(
                "wa_official.template_skipped_safety_gate",
                phone=_mask_phone(phone),
                template_name=template_name,
                whatsapp_send_enabled=False,
            )
            return ""

        self._check_circuit()
        to = phone.lstrip("+")
        components = []
        if params:
            components = [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in params],
                }
            ]
        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": "fr"},
                "components": components,
            },
        }
        url = f"{_GRAPH_API}/{self._phone_number_id}/messages"

        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=self._headers())
            resp.raise_for_status()
            data = resp.json()
            return (data.get("messages") or [{}])[0].get("id", "")
