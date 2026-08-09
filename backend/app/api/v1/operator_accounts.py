"""Administrator-only browser API for managing Operator accounts."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    get_browser_settings,
    require_capability,
    require_csrf,
    require_recent_reauthentication,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.config import Settings
from app.database import get_db
from app.models.operator_account import OperatorAccount
from app.operator_identity.accounts import (
    AccountStateError,
    AdministrativeAuthorization,
    AdministrativeAuthorizationDenied,
    ManagedOperatorNotFound,
    ManagedOperatorTargetDenied,
    OperatorAccountConflict,
    create_browser_managed_operator,
    disable_browser_managed_operator,
    enable_browser_managed_operator,
    set_browser_managed_operator_password,
)
from app.operator_identity.browser_sessions import SessionStoreUnavailable
from app.request_ids import normalize_or_generate_request_id
from app.schemas.operator_accounts import (
    OperatorAccountCreate,
    OperatorAccountListResponse,
    OperatorAccountSummary,
    OperatorPasswordSet,
)

router = APIRouter(prefix="/operator/accounts", tags=["operator-accounts"])
_MANAGE_CAPABILITY = "operator_account.manage"
_LIST_LIMIT = 200


def _request_id(request: Request) -> str:
    return normalize_or_generate_request_id(
        getattr(request.state, "request_id", None)
        or request.headers.get("X-Request-ID")
    )


def _authorization(request: Request, principal: BrowserPrincipal) -> AdministrativeAuthorization:
    return AdministrativeAuthorization(
        actor_account_id=principal.account.account_id,
        authorization_reference=_request_id(request),
        reason="Administrator-managed Operator account lifecycle",
    )


def _summary(account: OperatorAccount) -> OperatorAccountSummary:
    return OperatorAccountSummary(
        account_id=account.account_id,
        username=account.username_normalized,
        display_name=account.display_name,
        email=account.email_normalized,
        status=account.status,
        last_login_at=account.last_login_at,
        created_at=account.created_at,
    )


def _security_context(principal: BrowserPrincipal) -> dict[str, str]:
    return {
        "source_network_fingerprint": principal.session.record.ip_prefix_fingerprint,
        "user_agent_fingerprint": principal.session.record.user_agent_fingerprint,
    }


def _error(status_code: int, code: str, message: str) -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status_code,
        code=code,
        operator_code=code,
        message=message,
    )


def _map_service_error(exc: Exception) -> BrowserAuthError:
    if isinstance(exc, ManagedOperatorNotFound):
        return _error(404, "OPERATOR_ACCOUNT_NOT_FOUND", "The Operator account was not found.")
    if isinstance(exc, (ManagedOperatorTargetDenied, AdministrativeAuthorizationDenied)):
        return _error(403, "OPERATOR_ACCOUNT_TARGET_FORBIDDEN", "Only Operator accounts may be managed.")
    if isinstance(exc, OperatorAccountConflict):
        return _error(409, "OPERATOR_ACCOUNT_CONFLICT", "That username or email is already in use.")
    if isinstance(exc, AccountStateError):
        return _error(409, "OPERATOR_ACCOUNT_STATE_CONFLICT", "The Operator account state has changed.")
    if isinstance(exc, ValueError):
        return _error(422, "PASSWORD_POLICY_VIOLATION", "The password does not meet the password policy.")
    return _error(503, "SERVICE_UNAVAILABLE", "Operator account management is temporarily unavailable.")


async def _rollback_and_raise(db: AsyncSession, exc: Exception) -> None:
    await db.rollback()
    raise _map_service_error(exc) from exc


@router.get("", response_model=OperatorAccountListResponse)
async def list_operator_accounts(
    response: Response,
    _principal: Annotated[BrowserPrincipal, Depends(require_capability(_MANAGE_CAPABILITY))],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAccountListResponse:
    try:
        accounts = (
            await db.scalars(
                select(OperatorAccount)
                .where(OperatorAccount.role == "operator")
                .order_by(OperatorAccount.display_name, OperatorAccount.account_id)
                .limit(_LIST_LIMIT)
            )
        ).all()
    except (SQLAlchemyError, OSError) as exc:
        raise _map_service_error(exc) from exc
    response.headers["Cache-Control"] = "no-store"
    return OperatorAccountListResponse(items=[_summary(account) for account in accounts])


@router.post("", response_model=OperatorAccountSummary, status_code=status.HTTP_201_CREATED)
async def create_operator_account(
    body: OperatorAccountCreate,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability(_MANAGE_CAPABILITY))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAccountSummary:
    validate_state_changing_request(request, settings)
    password = body.password.get_secret_value()
    try:
        account = await create_browser_managed_operator(
            db,
            authorization=_authorization(request, principal),
            username=body.username,
            display_name=body.display_name,
            email=body.email,
            password=password,
            settings=settings,
            **_security_context(principal),
        )
        await db.commit()
    except (SQLAlchemyError, OSError, ValueError, OperatorAccountConflict, AdministrativeAuthorizationDenied) as exc:
        await _rollback_and_raise(db, exc)
    finally:
        password = ""
    response.headers["Cache-Control"] = "no-store"
    return _summary(account)


@router.post("/{account_id}/password", response_model=OperatorAccountSummary)
async def set_operator_password(
    account_id: UUID,
    body: OperatorPasswordSet,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability(_MANAGE_CAPABILITY))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAccountSummary:
    validate_state_changing_request(request, settings)
    password = body.new_password.get_secret_value()
    try:
        account = await set_browser_managed_operator_password(
            db,
            principal.session.state.sessions,
            authorization=_authorization(request, principal),
            account_id=account_id,
            password=password,
            settings=settings,
            **_security_context(principal),
        )
        await db.commit()
    except (
        SQLAlchemyError,
        OSError,
        ValueError,
        SessionStoreUnavailable,
        AccountStateError,
        ManagedOperatorNotFound,
        ManagedOperatorTargetDenied,
        AdministrativeAuthorizationDenied,
    ) as exc:
        await _rollback_and_raise(db, exc)
    finally:
        password = ""
    response.headers["Cache-Control"] = "no-store"
    return _summary(account)


@router.post("/{account_id}/disable", response_model=OperatorAccountSummary)
async def disable_operator(
    account_id: UUID,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability(_MANAGE_CAPABILITY))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAccountSummary:
    validate_state_changing_request(request, settings)
    try:
        account = await disable_browser_managed_operator(
            db,
            principal.session.state.sessions,
            authorization=_authorization(request, principal),
            account_id=account_id,
            settings=settings,
            **_security_context(principal),
        )
        await db.commit()
    except (
        SQLAlchemyError,
        OSError,
        SessionStoreUnavailable,
        AccountStateError,
        ManagedOperatorNotFound,
        ManagedOperatorTargetDenied,
        AdministrativeAuthorizationDenied,
    ) as exc:
        await _rollback_and_raise(db, exc)
    response.headers["Cache-Control"] = "no-store"
    return _summary(account)


@router.post("/{account_id}/enable", response_model=OperatorAccountSummary)
async def enable_operator(
    account_id: UUID,
    body: OperatorPasswordSet,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_capability(_MANAGE_CAPABILITY))],
    _csrf: Annotated[BrowserPrincipal, Depends(require_csrf)],
    _recent: Annotated[BrowserPrincipal, Depends(require_recent_reauthentication)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> OperatorAccountSummary:
    validate_state_changing_request(request, settings)
    password = body.new_password.get_secret_value()
    try:
        account = await enable_browser_managed_operator(
            db,
            principal.session.state.sessions,
            authorization=_authorization(request, principal),
            account_id=account_id,
            password=password,
            settings=settings,
            **_security_context(principal),
        )
        await db.commit()
    except (
        SQLAlchemyError,
        OSError,
        ValueError,
        SessionStoreUnavailable,
        AccountStateError,
        ManagedOperatorNotFound,
        ManagedOperatorTargetDenied,
        AdministrativeAuthorizationDenied,
    ) as exc:
        await _rollback_and_raise(db, exc)
    finally:
        password = ""
    response.headers["Cache-Control"] = "no-store"
    return _summary(account)
