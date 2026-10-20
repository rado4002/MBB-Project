"""Authoritative, commit-serialized inbound order for one conversation."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import Message


async def latest_inbound_message_id(
    session: AsyncSession, conversation_id: uuid.UUID,
) -> uuid.UUID | None:
    return await session.scalar(
        select(Message.message_id)
        .where(
            Message.conversation_id == conversation_id,
            Message.direction == "inbound",
        )
        .order_by(Message.inbound_receipt_order.desc())
        .limit(1)
    )
