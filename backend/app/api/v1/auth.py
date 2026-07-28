"""Default-off browser authentication API; never accepts legacy JWTs."""

from __future__ import annotations

import secrets
import time
import unicodedata
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Cookie, Depends, Header, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.browser_auth_deps import (
    BrowserPrincipal,
    BrowserSessionContext,
    capabilities_for,
    get_browser_auth_state,
    get_browser_settings,
    get_current_human,
    require_browser_auth_enabled,
    require_csrf,
    validate_state_changing_request,
)
from app.api.browser_auth_errors import BrowserAuthError
from app.api.deps import get_request_id
from app.config import Settings
from app.database import get_db
from app.models.operator_account import OperatorAccount, normalize_username
from app.operator_identity.audit import append_operator_audit_event
from app.operator_identity.browser_auth import (
    PREAUTH_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    BrowserAuthState,
)
from app.operator_identity.browser_sessions import (
    InvalidSessionToken,
    SessionStoreUnavailable,
)
from app.operator_identity.passwords import hash_password, verify_password
from app.schemas.auth import (
    BrowserSessionResponse,
    CsrfResponse,
    HumanSummary,
    LoginRequest,
    LogoutResponse,
    PasswordChangeRequest,
    PasswordRequest,
)

router = APIRouter(prefix="/auth", tags=["browser-auth"])
_DUMMY_PASSWORD_HASH = hash_password(secrets.token_urlsafe(32))


def _set_secure_cookie(
    response: Response, *, name: str, value: str, max_age: int
) -> None:
    response.set_cookie(
        key=name,
        value=value,
        max_age=max(0, max_age),
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _clear_secure_cookie(response: Response, name: str) -> None:
    response.delete_cookie(
        key=name,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )


def _session_response(
    principal: BrowserPrincipal,
    settings: Settings,
    *,
    csrf_token: str | None = None,
) -> BrowserSessionResponse:
    record = principal.session.record
    idle_expiry = min(
        record.last_activity_at_epoch + settings.browser_session_idle_seconds,
        record.absolute_expires_at_epoch,
    )
    recent_expiry = (
        record.recent_reauthenticated_at_epoch
        + settings.browser_recent_reauth_seconds
        if record.recent_reauthenticated_at_epoch
        else None
    )
    return BrowserSessionResponse(
        human=HumanSummary(
            account_id=str(principal.account.account_id),
            username=principal.account.username_normalized,
            display_name=principal.account.display_name,
            role=principal.account.role,
        ),
        capabilities=sorted(principal.capabilities),
        must_change_password=principal.account.must_change_password,
        idle_expires_at_epoch=idle_expiry,
        absolute_expires_at_epoch=record.absolute_expires_at_epoch,
        recent_reauthentication_expires_at_epoch=recent_expiry,
        csrf_token=csrf_token,
    )


def _source_context(request: Request, state: BrowserAuthState) -> tuple[str | None, str | None]:
    source = state.source_network(request.client.host if request.client else None)
    return source, request.headers.get("user-agent")


async def _audit(
    db: AsyncSession,
    *,
    state: BrowserAuthState,
    settings: Settings,
    request: Request,
    request_id: str,
    action: str,
    outcome: str,
    account: OperatorAccount | None = None,
    failure_code: str | None = None,
    metadata: dict | None = None,
) -> None:
    source, user_agent = _source_context(request, state)
    await append_operator_audit_event(
        db,
        category="security",
        actor_kind="human" if account else "unknown",
        actor_account_id=account.account_id if account else None,
        actor_display_name=account.display_name if account else None,
        effective_role=account.role if account else None,
        request_id=request_id,
        action=action,
        target_type="operator_account" if account else None,
        target_id=str(account.account_id) if account else None,
        outcome=outcome,
        failure_code=failure_code,
        metadata=metadata,
        source_network_fingerprint=state.fingerprint(source, purpose="network"),
        user_agent_fingerprint=state.fingerprint(
            user_agent, purpose="user-agent"
        ),
        settings=settings,
    )


def _raise_unavailable(_exc: Exception) -> BrowserAuthError:
    return BrowserAuthError(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        code="authentication_unavailable",
        message="Browser authentication is unavailable.",
    )


def _normalized_rate_identifier(username: str) -> tuple[str, str | None]:
    try:
        normalized = normalize_username(username)
    except (TypeError, ValueError):
        folded = unicodedata.normalize("NFKC", username).strip().lower()
        return folded[:128] or "invalid", None
    return normalized, normalized


def _temporary_credential_valid(account: OperatorAccount, now: datetime) -> bool:
    return not account.must_change_password or (
        account.temporary_password_expires_at is not None
        and account.temporary_password_expires_at > now
    )


def _validate_new_password(password: str, username: str) -> None:
    if not 12 <= len(password) <= 128:
        raise ValueError
    if any(unicodedata.category(character) == "Cc" for character in password):
        raise ValueError
    classes = (
        any(character.islower() for character in password),
        any(character.isupper() for character in password),
        any(character.isdigit() for character in password),
        any(not character.isalnum() for character in password),
    )
    if sum(classes) < 3 or username in password.lower():
        raise ValueError


@router.get("/csrf", response_model=CsrfResponse)
async def csrf(
    request: Request,
    response: Response,
    _enabled: Annotated[Settings, Depends(require_browser_auth_enabled)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    state: Annotated[BrowserAuthState, Depends(get_browser_auth_state)],
    db: Annotated[AsyncSession, Depends(get_db)],
    session_token: Annotated[
        str | None, Cookie(alias=SESSION_COOKIE_NAME, include_in_schema=False)
    ] = None,
) -> CsrfResponse:
    response.headers["Cache-Control"] = "no-store"
    source, user_agent = _source_context(request, state)
    if session_token:
        try:
            record = await state.sessions.get_session(session_token)
        except InvalidSessionToken:
            record = None
        except SessionStoreUnavailable as exc:
            raise _raise_unavailable(exc) from exc
        if record is not None:
            try:
                account = await db.scalar(
                    select(OperatorAccount).where(
                        OperatorAccount.account_id == record.account_id
                    )
                )
            except SQLAlchemyError as exc:
                raise _raise_unavailable(exc) from exc
            now = datetime.now(timezone.utc)
            valid = (
                account is not None
                and account.status == "active"
                and account.auth_version == record.auth_version
                and _temporary_credential_valid(account, now)
                and record.ip_prefix_fingerprint
                == state.fingerprint(source, purpose="network")
                and record.user_agent_fingerprint
                == state.fingerprint(user_agent, purpose="user-agent")
            )
            if valid:
                try:
                    activity = await state.sessions.update_activity(session_token)
                except SessionStoreUnavailable as exc:
                    raise _raise_unavailable(exc) from exc
                if activity.active:
                    if activity.updated:
                        record = await state.sessions.get_session(session_token)
                        if record is None:
                            raise BrowserAuthError(
                                status_code=status.HTTP_401_UNAUTHORIZED,
                                code="session_invalid",
                                message="The browser session is invalid or expired.",
                            )
                    _clear_secure_cookie(response, PREAUTH_COOKIE_NAME)
                    return CsrfResponse(
                        csrf_token=state.csrf_for_session(record),
                        expires_at_epoch=min(
                            record.last_activity_at_epoch
                            + settings.browser_session_idle_seconds,
                            record.absolute_expires_at_epoch,
                        ),
                    )
            try:
                await state.sessions.revoke_session(session_token)
            except (InvalidSessionToken, SessionStoreUnavailable):
                pass
        _clear_secure_cookie(response, SESSION_COOKIE_NAME)
    try:
        created = await state.create_preauth(
            source_network=source,
            user_agent=user_agent,
        )
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    _set_secure_cookie(
        response,
        name=PREAUTH_COOKIE_NAME,
        value=created.token,
        max_age=settings.browser_preauth_seconds,
    )
    return CsrfResponse(
        csrf_token=state.csrf_for_preauth(created.context),
        expires_at_epoch=created.context.expires_at_epoch,
    )


@router.post("/login", response_model=BrowserSessionResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    _enabled: Annotated[Settings, Depends(require_browser_auth_enabled)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    state: Annotated[BrowserAuthState, Depends(get_browser_auth_state)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    csrf_token: Annotated[
        str | None, Header(alias="X-CSRF-Token", include_in_schema=False)
    ] = None,
    preauth_token: Annotated[
        str | None, Cookie(alias=PREAUTH_COOKIE_NAME, include_in_schema=False)
    ] = None,
) -> BrowserSessionResponse:
    validate_state_changing_request(request, settings)
    source, user_agent = _source_context(request, state)
    try:
        context = (
            await state.get_preauth(
                preauth_token,
                source_network=source,
                user_agent=user_agent,
            )
            if preauth_token
            else None
        )
    except (InvalidSessionToken, SessionStoreUnavailable) as exc:
        if isinstance(exc, SessionStoreUnavailable):
            raise _raise_unavailable(exc) from exc
        context = None
    if context is None or not state.validate_csrf(
        csrf_token, state.csrf_for_preauth(context) if context else ""
    ):
        raise BrowserAuthError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="csrf_invalid",
            message="CSRF validation failed.",
        )
    rate_identifier, normalized_username = _normalized_rate_identifier(body.username)
    source_identifier = source or "unknown"
    try:
        account_count = await state.rate_count("account", rate_identifier)
        source_count = await state.rate_count("source", source_identifier)
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    if (
        account_count >= settings.browser_login_account_failure_limit
        or source_count >= settings.browser_login_source_failure_limit
    ):
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_login_throttled",
            outcome="denied",
            failure_code="authentication_throttled",
        )
        await db.commit()
        raise BrowserAuthError(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code="authentication_throttled",
            message="Authentication is temporarily unavailable.",
            retry_after_seconds=settings.browser_auth_rate_window_seconds,
        )
    try:
        account = (
            await db.scalar(
                select(OperatorAccount).where(
                    OperatorAccount.username_normalized == normalized_username
                )
            )
            if normalized_username
            else None
        )
    except SQLAlchemyError as exc:
        raise _raise_unavailable(exc) from exc
    password = body.password.get_secret_value()
    verified = verify_password(
        account.password_hash if account else _DUMMY_PASSWORD_HASH,
        password,
    )
    invalid = (
        account is None
        or account.status != "active"
        or not _temporary_credential_valid(account, datetime.now(timezone.utc))
        or not verified
    )
    if invalid:
        try:
            await state.record_rate_failure("account", rate_identifier)
            await state.record_rate_failure("source", source_identifier)
        except SessionStoreUnavailable as exc:
            raise _raise_unavailable(exc) from exc
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_login_failed",
            outcome="denied",
            account=account,
            failure_code="invalid_credentials",
        )
        await db.commit()
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_credentials",
            message="The username or password is invalid.",
        )
    try:
        await state.consume_preauth(preauth_token)
        created = await state.sessions.create_session(
            account_id=account.account_id,
            auth_version=account.auth_version,
            ip_prefix=source,
            user_agent=user_agent,
        )
        await state.clear_rate("account", rate_identifier)
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    account.last_login_at = datetime.now(timezone.utc)
    account.updated_at = account.last_login_at
    try:
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_login",
            outcome="succeeded",
            account=account,
            metadata={"oldest_session_evicted": bool(created.removed_session_refs)},
        )
        await db.commit()
    except SQLAlchemyError as exc:
        await state.sessions.revoke_session(created.token)
        raise _raise_unavailable(exc) from exc
    principal = BrowserPrincipal(
        account=account,
        session=BrowserSessionContext(
            raw_token=created.token, record=created.record, state=state
        ),
        capabilities=capabilities_for(account),
    )
    _set_secure_cookie(
        response,
        name=SESSION_COOKIE_NAME,
        value=created.token,
        max_age=created.record.absolute_expires_at_epoch - int(time.time()),
    )
    _clear_secure_cookie(response, PREAUTH_COOKIE_NAME)
    response.headers["Cache-Control"] = "no-store"
    return _session_response(
        principal,
        settings,
        csrf_token=state.csrf_for_session(created.record),
    )


@router.get(
    "/session",
    response_model=BrowserSessionResponse,
    response_model_exclude_none=True,
)
async def current_session(
    principal: Annotated[BrowserPrincipal, Depends(get_current_human)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    response: Response,
) -> BrowserSessionResponse:
    response.headers["Cache-Control"] = "no-store"
    return _session_response(principal, settings)


@router.post("/logout", response_model=LogoutResponse)
async def logout(
    request: Request,
    response: Response,
    _enabled: Annotated[Settings, Depends(require_browser_auth_enabled)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    state: Annotated[BrowserAuthState, Depends(get_browser_auth_state)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
    csrf_token: Annotated[
        str | None, Header(alias="X-CSRF-Token", include_in_schema=False)
    ] = None,
    raw_token: Annotated[
        str | None, Cookie(alias=SESSION_COOKIE_NAME, include_in_schema=False)
    ] = None,
) -> LogoutResponse:
    validate_state_changing_request(request, settings)
    record = None
    if raw_token:
        try:
            record = await state.sessions.get_session(raw_token)
        except InvalidSessionToken:
            record = None
        except SessionStoreUnavailable as exc:
            raise _raise_unavailable(exc) from exc
    if record:
        if not state.validate_csrf(csrf_token, state.csrf_for_session(record)):
            raise BrowserAuthError(
                status_code=status.HTTP_403_FORBIDDEN,
                code="csrf_invalid",
                message="CSRF validation failed.",
            )
        try:
            account = await db.scalar(
                select(OperatorAccount).where(
                    OperatorAccount.account_id == record.account_id
                )
            )
        except SQLAlchemyError as exc:
            raise _raise_unavailable(exc) from exc
        try:
            await state.sessions.revoke_session(raw_token)
        except SessionStoreUnavailable as exc:
            raise _raise_unavailable(exc) from exc
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_logout",
            outcome="succeeded",
            account=account,
        )
        await db.commit()
    _clear_secure_cookie(response, SESSION_COOKIE_NAME)
    _clear_secure_cookie(response, PREAUTH_COOKIE_NAME)
    response.headers["Cache-Control"] = "no-store"
    return LogoutResponse()


@router.post("/reauthenticate", response_model=BrowserSessionResponse)
async def reauthenticate(
    body: PasswordRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> BrowserSessionResponse:
    validate_state_changing_request(request, settings)
    if "auth.reauthenticate" not in principal.capabilities:
        raise BrowserAuthError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="capability_required",
            message="This browser session is not permitted to perform that action.",
        )
    state = principal.session.state
    rate_value = principal.session.record.session_ref
    try:
        if (
            await state.rate_count("reauth", rate_value)
            >= settings.browser_reauth_failure_limit
        ):
            await _audit(
                db,
                state=state,
                settings=settings,
                request=request,
                request_id=request_id,
                action="browser_reauthentication_throttled",
                outcome="denied",
                account=principal.account,
                failure_code="authentication_throttled",
            )
            await db.commit()
            raise BrowserAuthError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="authentication_throttled",
                message="Authentication is temporarily unavailable.",
                retry_after_seconds=settings.browser_auth_rate_window_seconds,
            )
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    if not verify_password(
        principal.account.password_hash, body.password.get_secret_value()
    ):
        try:
            await state.record_rate_failure("reauth", rate_value)
        except SessionStoreUnavailable as exc:
            raise _raise_unavailable(exc) from exc
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_reauthentication_failed",
            outcome="denied",
            account=principal.account,
            failure_code="invalid_credentials",
        )
        await db.commit()
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_credentials",
            message="The password is invalid.",
        )
    now = int(time.time())
    try:
        rotated = await state.sessions.rotate_session(
            principal.session.raw_token,
            now_epoch=now,
            recent_reauthenticated_at_epoch=now,
        )
        if rotated is None:
            raise BrowserAuthError(
                status_code=status.HTTP_401_UNAUTHORIZED,
                code="session_invalid",
                message="The browser session is invalid or expired.",
            )
        record = await state.sessions.get_session(rotated.token, now_epoch=now)
        if record is None:
            raise SessionStoreUnavailable("rotated session was not persisted")
        await state.clear_rate("reauth", rate_value)
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    try:
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_reauthentication",
            outcome="succeeded",
            account=principal.account,
        )
        await db.commit()
    except SQLAlchemyError as exc:
        await state.sessions.revoke_session(rotated.token)
        raise _raise_unavailable(exc) from exc
    principal = BrowserPrincipal(
        account=principal.account,
        session=principal.session.__class__(
            raw_token=rotated.token, record=record, state=state
        ),
        capabilities=principal.capabilities,
    )
    _set_secure_cookie(
        response,
        name=SESSION_COOKIE_NAME,
        value=rotated.token,
        max_age=record.absolute_expires_at_epoch - now,
    )
    response.headers["Cache-Control"] = "no-store"
    return _session_response(
        principal, settings, csrf_token=state.csrf_for_session(record)
    )


@router.post("/password/change", response_model=BrowserSessionResponse)
async def change_password(
    body: PasswordChangeRequest,
    request: Request,
    response: Response,
    principal: Annotated[BrowserPrincipal, Depends(require_csrf)],
    settings: Annotated[Settings, Depends(get_browser_settings)],
    db: Annotated[AsyncSession, Depends(get_db)],
    request_id: Annotated[str, Depends(get_request_id)],
) -> BrowserSessionResponse:
    validate_state_changing_request(request, settings)
    state = principal.session.state
    rate_value = principal.session.record.session_ref
    current_password = body.current_password.get_secret_value()
    new_password = body.new_password.get_secret_value()
    try:
        if (
            await state.rate_count("reauth", rate_value)
            >= settings.browser_reauth_failure_limit
        ):
            await _audit(
                db,
                state=state,
                settings=settings,
                request=request,
                request_id=request_id,
                action="browser_password_change_throttled",
                outcome="denied",
                account=principal.account,
                failure_code="authentication_throttled",
            )
            await db.commit()
            raise BrowserAuthError(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                code="authentication_throttled",
                message="Authentication is temporarily unavailable.",
                retry_after_seconds=settings.browser_auth_rate_window_seconds,
            )
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    if not verify_password(principal.account.password_hash, current_password):
        try:
            await state.record_rate_failure("reauth", rate_value)
        except SessionStoreUnavailable as exc:
            raise _raise_unavailable(exc) from exc
        await _audit(
            db,
            state=state,
            settings=settings,
            request=request,
            request_id=request_id,
            action="browser_password_change_failed",
            outcome="denied",
            account=principal.account,
            failure_code="invalid_credentials",
        )
        await db.commit()
        raise BrowserAuthError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="invalid_credentials",
            message="The current password is invalid.",
        )
    try:
        _validate_new_password(new_password, principal.account.username_normalized)
        if verify_password(principal.account.password_hash, new_password):
            raise ValueError
    except ValueError as exc:
        raise BrowserAuthError(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="password_policy_violation",
            message="The new password does not meet the password policy.",
        ) from exc
    now_dt = datetime.now(timezone.utc)
    principal.account.password_hash = hash_password(new_password)
    principal.account.must_change_password = False
    principal.account.temporary_password_expires_at = None
    principal.account.password_changed_at = now_dt
    principal.account.updated_at = now_dt
    principal.account.auth_version += 1
    await _audit(
        db,
        state=state,
        settings=settings,
        request=request,
        request_id=request_id,
        action="browser_password_changed",
        outcome="succeeded",
        account=principal.account,
    )
    try:
        await state.sessions.revoke_all_sessions(principal.account.account_id)
        now = int(time.time())
        source, user_agent = _source_context(request, state)
        created = await state.sessions.create_session(
            account_id=principal.account.account_id,
            auth_version=principal.account.auth_version,
            recent_reauthenticated_at_epoch=now,
            ip_prefix=source,
            user_agent=user_agent,
            now_epoch=now,
        )
        await state.clear_rate("reauth", rate_value)
    except SessionStoreUnavailable as exc:
        raise _raise_unavailable(exc) from exc
    try:
        await db.commit()
    except SQLAlchemyError as exc:
        await state.sessions.revoke_session(created.token)
        raise _raise_unavailable(exc) from exc
    principal = BrowserPrincipal(
        account=principal.account,
        session=principal.session.__class__(
            raw_token=created.token, record=created.record, state=state
        ),
        capabilities=capabilities_for(principal.account),
    )
    _set_secure_cookie(
        response,
        name=SESSION_COOKIE_NAME,
        value=created.token,
        max_age=created.record.absolute_expires_at_epoch - now,
    )
    response.headers["Cache-Control"] = "no-store"
    return _session_response(
        principal,
        settings,
        csrf_token=state.csrf_for_session(created.record),
    )
