"""Browser-only authentication, authorization, CSRF and origin dependencies."""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import Cookie, Depends, Header, Request, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings, get_settings
from app.database import get_db
from app.models.operator_account import OperatorAccount
from app.operator_identity.browser_auth import BrowserAuthState, SESSION_COOKIE_NAME
from app.operator_identity.browser_sessions import (
    BrowserSessionRecord,
    InvalidSessionToken,
    SessionStoreUnavailable,
    get_browser_session_client,
)

TEMPORARY_CAPABILITIES = frozenset(
    {
        "auth.csrf.read",
        "auth.logout",
        "auth.password.change",
        "auth.session.read",
    }
)
BASE_CAPABILITIES = TEMPORARY_CAPABILITIES | {"auth.reauthenticate"}
ROLE_CAPABILITIES = {
    "administrator": BASE_CAPABILITIES
    | {
        "conversation.read",
        "message.read",
        "message.reply",
        "internal_note.read",
        "internal_note.create",
        "escalation.create",
        "conversation.ownership.change",
    },
    "operator": BASE_CAPABILITIES
    | {
        "conversation.read",
        "message.read",
        "message.reply",
        "internal_note.read",
        "internal_note.create",
        "escalation.create",
        "conversation.ownership.change",
    },
    "analyst": BASE_CAPABILITIES,
}


@dataclass(frozen=True)
class BrowserSessionContext:
    raw_token: str = field(repr=False)
    record: BrowserSessionRecord
    state: BrowserAuthState = field(repr=False)


@dataclass(frozen=True)
class BrowserPrincipal:
    account: OperatorAccount = field(repr=False)
    session: BrowserSessionContext = field(repr=False)
    capabilities: frozenset[str]


def get_browser_settings() -> Settings:
    return get_settings()


def get_browser_redis(
    settings: Annotated[Settings, Depends(get_browser_settings)],
) -> Any:
    return get_browser_session_client(settings)


def get_browser_auth_state(
    redis_client: Annotated[Any, Depends(get_browser_redis)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
) -> BrowserAuthState:
    return BrowserAuthState(redis_client=redis_client, settings=settings)


def require_browser_auth_enabled(
    settings: Annotated[Settings, Depends(get_browser_settings)],
) -> Settings:
    if not settings.browser_auth_enabled:
        raise BrowserAuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="browser_auth_disabled",
            message="Browser authentication is unavailable.",
        )
    return settings


def capabilities_for(account: OperatorAccount) -> frozenset[str]:
    if account.must_change_password:
        return TEMPORARY_CAPABILITIES
    return frozenset(ROLE_CAPABILITIES.get(account.role, BASE_CAPABILITIES))


def validate_state_changing_request(request: Request, settings: Settings) -> None:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type.lower() != "application/json":
        raise BrowserAuthError(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            code="json_required",
            message="This request requires JSON.",
        )
    fetch_site = request.headers.get("sec-fetch-site")
    if fetch_site and fetch_site != "same-origin":
        raise BrowserAuthError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="origin_invalid",
            message="Request origin was rejected.",
        )
    allowed = settings.browser_allowed_origin
    parsed = urlsplit(allowed)
    if (
        not allowed
        or parsed.scheme != "https"
        or not parsed.netloc
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise BrowserAuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="authentication_unavailable",
            message="Browser authentication is unavailable.",
        )
    origin = request.headers.get("origin")
    if origin is not None:
        valid = origin == allowed
    else:
        referer = request.headers.get("referer")
        if not referer:
            valid = False
        else:
            ref = urlsplit(referer)
            valid = f"{ref.scheme}://{ref.netloc}" == allowed
    if not valid:
        raise BrowserAuthError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="origin_invalid",
            message="Request origin was rejected.",
        )


async def get_browser_session(
    _enabled: Annotated[Settings, Depends(require_browser_auth_enabled)],
    state: Annotated[BrowserAuthState, Depends(get_browser_auth_state)],
    raw_token: Annotated[
        str | None, Cookie(alias=SESSION_COOKIE_NAME, include_in_schema=False)
    ] = None,
) -> BrowserSessionContext:
    if not raw_token:
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="session_required",
            message="An active browser session is required.",
            operator_code="AUTH_SESSION_EXPIRED",
        )
    try:
        record = await state.sessions.get_session(raw_token)
    except InvalidSessionToken:
        record = None
    except SessionStoreUnavailable as exc:
        raise BrowserAuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="authentication_unavailable",
            message="Browser authentication is unavailable.",
        ) from exc
    if record is None:
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="session_invalid",
            message="The browser session is invalid or expired.",
            operator_code="AUTH_SESSION_EXPIRED",
        )
    return BrowserSessionContext(raw_token=raw_token, record=record, state=state)


async def get_current_human(
    request: Request,
    session_context: Annotated[BrowserSessionContext, Depends(get_browser_session)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> BrowserPrincipal:
    try:
        account = await db.scalar(
            select(OperatorAccount).where(
                OperatorAccount.account_id == session_context.record.account_id
            )
        )
    except (SQLAlchemyError, OSError) as exc:
        raise BrowserAuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="authentication_unavailable",
            message="Browser authentication is unavailable.",
        ) from exc
    now = datetime.now(timezone.utc)
    if account is not None and account.status != "active":
        try:
            await session_context.state.sessions.revoke_session(
                session_context.raw_token
            )
        except (InvalidSessionToken, SessionStoreUnavailable):
            pass
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="session_invalid",
            message="The browser session is invalid or expired.",
            operator_code="AUTH_ACCOUNT_DISABLED",
        )
    invalid = (
        account is None
        or account.auth_version != session_context.record.auth_version
        or (
            account.must_change_password
            and (
                account.temporary_password_expires_at is None
                or account.temporary_password_expires_at <= now
            )
        )
    )
    source_network = session_context.state.source_network(
        request.client.host if request.client else None
    )
    expected_network = session_context.state.fingerprint(
        source_network, purpose="network"
    )
    expected_agent = session_context.state.fingerprint(
        request.headers.get("user-agent"), purpose="user-agent"
    )
    invalid = invalid or (
        session_context.record.ip_prefix_fingerprint != expected_network
        or session_context.record.user_agent_fingerprint != expected_agent
    )
    if invalid:
        try:
            await session_context.state.sessions.revoke_session(
                session_context.raw_token
            )
        except (InvalidSessionToken, SessionStoreUnavailable):
            pass
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="session_invalid",
            message="The browser session is invalid or expired.",
            operator_code="AUTH_SESSION_EXPIRED",
        )
    try:
        activity = await session_context.state.sessions.update_activity(
            session_context.raw_token
        )
    except SessionStoreUnavailable as exc:
        raise BrowserAuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="authentication_unavailable",
            message="Browser authentication is unavailable.",
        ) from exc
    if not activity.active:
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="session_invalid",
            message="The browser session is invalid or expired.",
            operator_code="AUTH_SESSION_EXPIRED",
        )
    record = session_context.record
    if activity.updated:
        record = replace(record, last_activity_at_epoch=int(time.time()))
        session_context = replace(session_context, record=record)
    return BrowserPrincipal(
        account=account,
        session=session_context,
        capabilities=capabilities_for(account),
    )


def require_capability(capability: str):
    async def _require(
        principal: Annotated[BrowserPrincipal, Depends(get_current_human)],
    ) -> BrowserPrincipal:
        if capability not in principal.capabilities:
            raise BrowserAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="capability_required",
                message="This browser session is not permitted to perform that action.",
            )
        return principal

    return _require


async def require_recent_reauthentication(
    principal: Annotated[BrowserPrincipal, Depends(get_current_human)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
) -> BrowserPrincipal:
    if (
        principal.session.record.recent_reauthenticated_at_epoch
        + settings.browser_recent_reauth_seconds
        < int(time.time())
    ):
        raise BrowserAuthError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="recent_reauthentication_required",
            message="Recent password confirmation is required.",
        )
    return principal


async def require_csrf(
    principal: Annotated[BrowserPrincipal, Depends(get_current_human)],
    csrf_token: Annotated[
        str | None, Header(alias="X-CSRF-Token", include_in_schema=False)
    ] = None,
) -> BrowserPrincipal:
    expected = principal.session.state.csrf_for_session(principal.session.record)
    if not principal.session.state.validate_csrf(csrf_token, expected):
        raise BrowserAuthError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="csrf_invalid",
            message="CSRF validation failed.",
        )
    return principal
