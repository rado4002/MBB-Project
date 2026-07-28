"""
FastAPI dependency functions shared across all routers.

Usage:
    from app.api.deps import get_current_role, require_role, DBSession, RedisClient
"""
from typing import Annotated

import structlog
from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.redis_client import get_redis
from app.request_ids import normalize_or_generate_request_id
from app.security import decode_token

log = structlog.get_logger()
bearer_scheme = HTTPBearer(auto_error=False)

# ── Type aliases ──────────────────────────────────────────────────────────────
DBSession = Annotated[AsyncSession, Depends(get_db)]
RedisClient = Annotated[object, Depends(get_redis)]


# ── JWT Auth ──────────────────────────────────────────────────────────────────
async def get_current_role(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> str:
    """Validate JWT Bearer token. Returns the caller's role string."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing_token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    payload = decode_token(credentials.credentials)
    return payload.get("role", "")


CurrentRole = Annotated[str, Depends(get_current_role)]


def require_role(*allowed_roles: str):
    """
    Dependency factory: enforces that the caller's JWT role is in allowed_roles.

    Usage:
        @router.put("/...", dependencies=[Depends(require_role("admin"))])
    """
    async def _check(role: CurrentRole):
        if role not in allowed_roles:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"role_not_permitted. Required: {list(allowed_roles)}",
            )
    return _check


# ── Request Tracing ───────────────────────────────────────────────────────────
async def get_request_id(x_request_id: Annotated[str | None, Header()] = None) -> str:
    """Return the incoming X-Request-ID or generate a new UUID."""
    return normalize_or_generate_request_id(x_request_id)


RequestID = Annotated[str, Depends(get_request_id)]


# ── Idempotency Key ───────────────────────────────────────────────────────────
async def get_idempotency_key(
    x_idempotency_key: Annotated[str | None, Header()] = None,
) -> str | None:
    return x_idempotency_key


IdempotencyKey = Annotated[str | None, Depends(get_idempotency_key)]
