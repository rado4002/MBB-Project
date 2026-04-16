"""
app/modules/m3_queue/recovery.py — Recovery message sender.

After a blackout, sends a "we're back" notification to each affected
customer in their detected language before processing their queued messages.
"""
from __future__ import annotations

import structlog

from app.i18n.messages import t

log = structlog.get_logger(__name__)


async def send_recovery_message(phone: str, language: str = "french") -> None:
    """
    Send a recovery notification to a customer after blackout.
    Best-effort: failures are logged, never raised.
    """
    from app.adapters import get_messaging_adapter

    msg = t("recovery", language)
    try:
        adapter = get_messaging_adapter()
        await adapter.send_message(phone, msg)
        log.info("m3.recovery_sent", phone=phone, language=language)
    except Exception as exc:
        log.error("m3.recovery_send_failed", phone=phone, error=str(exc))
