"""Shared human Administrator context for commerce-domain commands."""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.operator_account import OperatorAccount


class CommerceAuthorizationDenied(Exception):
    """Raised before mutation when the actor is not an active Administrator."""


@dataclass(frozen=True)
class CommerceAdminContext:
    actor_account_id: uuid.UUID
    request_id: str
    source_network_fingerprint: str | None = None
    user_agent_fingerprint: str | None = None


async def require_commerce_administrator(
    session: AsyncSession, context: CommerceAdminContext
) -> OperatorAccount:
    actor = await session.get(OperatorAccount, context.actor_account_id)
    if actor is None or actor.role != "administrator" or actor.status != "active":
        raise CommerceAuthorizationDenied(
            "an active Administrator must authorize commerce maintenance"
        )
    return actor
