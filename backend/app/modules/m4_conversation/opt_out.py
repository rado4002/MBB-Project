"""
app/modules/m4_conversation/opt_out.py — Opt-out detection and handling.

Re-exports opt-out detection from m1_gateway.language_detector and adds
the full opt-out flow: detect → flag customer → close conversation → ack.
"""
from __future__ import annotations

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import Conversation
from app.models.customer import Customer
from app.modules.m1_gateway.language_detector import is_opt_out  # noqa: F401  re-export

log = structlog.get_logger(__name__)


async def handle_opt_out(
    *,
    session: AsyncSession,
    customer_phone: str,
    conversation_id: str,
    language: str,
) -> None:
    """
    Execute the full opt-out flow:
      1. Set customer.opt_out_flag = True
      2. Transition conversation → dormant
      3. Send opt-out acknowledgment (caller's responsibility)
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)

    # Flag customer
    await session.execute(
        update(Customer)
        .where(Customer.phone_number == customer_phone)
        .values(opt_out_flag=True, opt_out_at=now)
    )

    # Close conversation
    await session.execute(
        update(Conversation)
        .where(Conversation.conversation_id == conversation_id)
        .values(status="dormant", updated_at=now)
    )

    log.info(
        "m4.opt_out_processed",
        phone=customer_phone,
        conv_id=conversation_id,
        language=language,
    )
