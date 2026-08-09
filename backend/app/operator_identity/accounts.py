"""Transactional internal services for operator-account lifecycle commands."""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.operator_account import (
    OperatorAccount,
    normalize_display_name,
    normalize_email,
    normalize_username,
)
from app.operator_identity.audit import append_operator_audit_event
from app.operator_identity.browser_sessions import BrowserSessionStore
from app.operator_identity.passwords import hash_password, validate_user_chosen_password

ALLOWED_ROLES = frozenset({"administrator", "operator", "analyst"})


class OperatorAccountError(RuntimeError):
    pass


class BootstrapUnavailable(OperatorAccountError):
    pass


class AdministrativeAuthorizationDenied(OperatorAccountError):
    pass


class AccountStateError(OperatorAccountError):
    pass


class OperatorAccountConflict(OperatorAccountError):
    pass


class ManagedOperatorNotFound(OperatorAccountError):
    pass


class ManagedOperatorTargetDenied(OperatorAccountError):
    pass


@dataclass(frozen=True)
class AdministrativeAuthorization:
    actor_account_id: uuid.UUID
    authorization_reference: str
    reason: str

    def __post_init__(self) -> None:
        if not self.authorization_reference.strip():
            raise ValueError("authorization reference is required")
        if not self.reason.strip():
            raise ValueError("authorization reason is required")


@dataclass
class IssuedTemporaryCredential:
    account: OperatorAccount
    _plaintext: str | None = field(repr=False)

    def consume(self) -> str:
        """Return the temporary credential once, then discard this handle's copy."""
        if self._plaintext is None:
            raise RuntimeError("temporary credential has already been consumed")
        plaintext = self._plaintext
        self._plaintext = None
        return plaintext


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _temporary_password() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(16)).rstrip(b"=").decode()


def _temporary_expiry(now: datetime, settings: Settings) -> datetime:
    return now + timedelta(seconds=settings.temporary_password_lifetime_seconds)


async def _load_account_by_username(
    session: AsyncSession, username: str
) -> OperatorAccount:
    normalized = normalize_username(username)
    result = await session.execute(
        select(OperatorAccount).where(
            OperatorAccount.username_normalized == normalized
        )
    )
    account = result.scalar_one_or_none()
    if account is None:
        raise AccountStateError("operator account was not found")
    return account


async def _require_active_administrator(
    session: AsyncSession, authorization: AdministrativeAuthorization
) -> OperatorAccount:
    actor = await session.get(OperatorAccount, authorization.actor_account_id)
    if (
        actor is None
        or actor.role != "administrator"
        or actor.status != "active"
    ):
        raise AdministrativeAuthorizationDenied(
            "an active administrator account must authorize this command"
        )
    return actor


def _authorization_metadata(
    authorization: AdministrativeAuthorization,
) -> dict[str, str]:
    return {"administrative_authorization_reason": authorization.reason.strip()}


async def bootstrap_first_administrator(
    session: AsyncSession,
    *,
    username: str,
    display_name: str,
    email: str | None,
    human_operator_label: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> IssuedTemporaryCredential:
    """Create the first administrator only while the account table is empty."""
    normalized_operator_label = normalize_display_name(human_operator_label)
    configured = settings or get_settings()
    event_time = now or _utcnow()

    # Serializes concurrent empty-table checks without inserting sentinel data.
    await session.execute(
        text("LOCK TABLE mbb.operator_accounts IN SHARE ROW EXCLUSIVE MODE")
    )
    count_result = await session.execute(select(func.count()).select_from(OperatorAccount))
    if int(count_result.scalar_one()) != 0:
        raise BootstrapUnavailable("first-administrator bootstrap is permanently unavailable")

    temporary_password = _temporary_password()
    account = OperatorAccount(
        username_normalized=username,
        display_name=display_name,
        email_normalized=email,
        password_hash=hash_password(temporary_password),
        role="administrator",
        status="active",
        auth_version=1,
        must_change_password=True,
        temporary_password_expires_at=_temporary_expiry(event_time, configured),
    )
    session.add(account)
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="bootstrap",
        actor_display_name=normalized_operator_label,
        action="operator_account.bootstrap_created",
        target_type="operator_account",
        target_id=str(account.account_id),
        outcome="succeeded",
        metadata={"created_role": "administrator"},
        occurred_at=event_time,
        settings=configured,
    )
    return IssuedTemporaryCredential(account=account, _plaintext=temporary_password)


async def provision_operator_account(
    session: AsyncSession,
    *,
    authorization: AdministrativeAuthorization,
    username: str,
    display_name: str,
    email: str | None,
    role: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> IssuedTemporaryCredential:
    if role not in ALLOWED_ROLES:
        raise ValueError("unsupported operator role")
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    temporary_password = _temporary_password()
    account = OperatorAccount(
        username_normalized=username,
        display_name=display_name,
        email_normalized=email,
        password_hash=hash_password(temporary_password),
        role=role,
        status="active",
        auth_version=1,
        must_change_password=True,
        temporary_password_expires_at=_temporary_expiry(event_time, configured),
    )
    session.add(account)
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.provisioned",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="administrative_cli_authorization",
        outcome="succeeded",
        metadata={
            **_authorization_metadata(authorization),
            "created_role": role,
        },
        occurred_at=event_time,
        settings=configured,
    )
    return IssuedTemporaryCredential(account=account, _plaintext=temporary_password)


async def reset_operator_password(
    session: AsyncSession,
    session_store: BrowserSessionStore,
    *,
    authorization: AdministrativeAuthorization,
    username: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> IssuedTemporaryCredential:
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    account = await _load_account_by_username(session, username)
    temporary_password = _temporary_password()

    # Redis revocation precedes the DB change. A later DB rollback may log the
    # operator out, but can never leave a stale session authorized.
    await session_store.revoke_all_sessions(account.account_id)
    account.password_hash = hash_password(temporary_password)
    account.must_change_password = True
    account.temporary_password_expires_at = _temporary_expiry(event_time, configured)
    account.password_changed_at = event_time
    account.auth_version += 1
    account.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.password_reset",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="administrative_cli_authorization",
        outcome="succeeded",
        metadata=_authorization_metadata(authorization),
        occurred_at=event_time,
        settings=configured,
    )
    return IssuedTemporaryCredential(account=account, _plaintext=temporary_password)


async def disable_operator_account(
    session: AsyncSession,
    session_store: BrowserSessionStore,
    *,
    authorization: AdministrativeAuthorization,
    username: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> OperatorAccount:
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    account = await _load_account_by_username(session, username)
    if account.status == "disabled":
        raise AccountStateError("operator account is already disabled")
    await session_store.revoke_all_sessions(account.account_id)
    account.status = "disabled"
    account.auth_version += 1
    account.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.disabled",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="administrative_cli_authorization",
        outcome="succeeded",
        metadata=_authorization_metadata(authorization),
        occurred_at=event_time,
        settings=configured,
    )
    return account


async def reactivate_operator_account(
    session: AsyncSession,
    session_store: BrowserSessionStore,
    *,
    authorization: AdministrativeAuthorization,
    username: str,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> IssuedTemporaryCredential:
    """Reactivate only while issuing a fresh temporary credential in one operation."""
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    account = await _load_account_by_username(session, username)
    if account.status != "disabled":
        raise AccountStateError("only a disabled operator account can be reactivated")
    temporary_password = _temporary_password()
    await session_store.revoke_all_sessions(account.account_id)
    account.status = "active"
    account.password_hash = hash_password(temporary_password)
    account.must_change_password = True
    account.temporary_password_expires_at = _temporary_expiry(event_time, configured)
    account.password_changed_at = event_time
    account.auth_version += 1
    account.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.reactivated_with_password_reset",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="administrative_cli_authorization",
        outcome="succeeded",
        metadata=_authorization_metadata(authorization),
        occurred_at=event_time,
        settings=configured,
    )
    return IssuedTemporaryCredential(account=account, _plaintext=temporary_password)


async def _load_managed_operator(
    session: AsyncSession, account_id: uuid.UUID
) -> OperatorAccount:
    account = await session.scalar(
        select(OperatorAccount)
        .where(OperatorAccount.account_id == account_id)
        .with_for_update()
    )
    if account is None:
        raise ManagedOperatorNotFound("operator account was not found")
    if account.role != "operator":
        raise ManagedOperatorTargetDenied("only Operator accounts can be managed")
    return account


def _browser_audit_metadata(
    authorization: AdministrativeAuthorization,
    *,
    before_status: str | None = None,
    after_status: str | None = None,
    before_auth_version: int | None = None,
    after_auth_version: int | None = None,
    revoked_session_count: int | None = None,
) -> dict[str, str | int]:
    metadata: dict[str, str | int] = {
        "source": "operator_browser",
        **_authorization_metadata(authorization),
    }
    if before_status is not None:
        metadata["previous_status"] = before_status
    if after_status is not None:
        metadata["new_status"] = after_status
    if before_auth_version is not None:
        metadata["previous_auth_version"] = before_auth_version
    if after_auth_version is not None:
        metadata["new_auth_version"] = after_auth_version
    if revoked_session_count is not None:
        metadata["revoked_session_count"] = revoked_session_count
    return metadata


async def create_browser_managed_operator(
    session: AsyncSession,
    *,
    authorization: AdministrativeAuthorization,
    username: str,
    display_name: str,
    email: str | None,
    password: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> OperatorAccount:
    """Create an active Operator with a user-chosen password from the browser UI."""
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    normalized_username = normalize_username(username)
    normalized_display_name = normalize_display_name(display_name)
    normalized_email = normalize_email(email)
    validate_user_chosen_password(
        password,
        username=normalized_username,
        display_name=normalized_display_name,
    )
    account = OperatorAccount(
        username_normalized=normalized_username,
        display_name=normalized_display_name,
        email_normalized=normalized_email,
        password_hash=hash_password(password),
        role="operator",
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=event_time,
        updated_at=event_time,
    )
    try:
        async with session.begin_nested():
            session.add(account)
            await session.flush()
    except IntegrityError as exc:
        raise OperatorAccountConflict(
            "an Operator with that username or email already exists"
        ) from exc
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.provisioned",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="operator_browser",
        outcome="succeeded",
        metadata={
            **_browser_audit_metadata(
                authorization,
                after_status=account.status,
                after_auth_version=account.auth_version,
            ),
            "created_role": "operator",
        },
        source_network_fingerprint=source_network_fingerprint,
        user_agent_fingerprint=user_agent_fingerprint,
        occurred_at=event_time,
        settings=configured,
    )
    return account


async def set_browser_managed_operator_password(
    session: AsyncSession,
    session_store: BrowserSessionStore,
    *,
    authorization: AdministrativeAuthorization,
    account_id: uuid.UUID,
    password: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> OperatorAccount:
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    account = await _load_managed_operator(session, account_id)
    if account.status != "active":
        raise AccountStateError("only an active Operator password can be changed")
    validate_user_chosen_password(
        password,
        username=account.username_normalized,
        display_name=account.display_name,
        current_password_hash=account.password_hash,
    )
    before_version = account.auth_version
    revoked = await session_store.revoke_all_sessions(account.account_id)
    account.password_hash = hash_password(password)
    account.must_change_password = False
    account.temporary_password_expires_at = None
    account.password_changed_at = event_time
    account.auth_version += 1
    account.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.password_reset",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="operator_browser",
        outcome="succeeded",
        metadata=_browser_audit_metadata(
            authorization,
            before_status=account.status,
            after_status=account.status,
            before_auth_version=before_version,
            after_auth_version=account.auth_version,
            revoked_session_count=len(revoked),
        ),
        source_network_fingerprint=source_network_fingerprint,
        user_agent_fingerprint=user_agent_fingerprint,
        occurred_at=event_time,
        settings=configured,
    )
    return account


async def disable_browser_managed_operator(
    session: AsyncSession,
    session_store: BrowserSessionStore,
    *,
    authorization: AdministrativeAuthorization,
    account_id: uuid.UUID,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> OperatorAccount:
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    account = await _load_managed_operator(session, account_id)
    if account.status != "active":
        raise AccountStateError("Operator account is already disabled")
    before_version = account.auth_version
    revoked = await session_store.revoke_all_sessions(account.account_id)
    account.status = "disabled"
    account.auth_version += 1
    account.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.disabled",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="operator_browser",
        outcome="succeeded",
        metadata=_browser_audit_metadata(
            authorization,
            before_status="active",
            after_status=account.status,
            before_auth_version=before_version,
            after_auth_version=account.auth_version,
            revoked_session_count=len(revoked),
        ),
        source_network_fingerprint=source_network_fingerprint,
        user_agent_fingerprint=user_agent_fingerprint,
        occurred_at=event_time,
        settings=configured,
    )
    return account


async def enable_browser_managed_operator(
    session: AsyncSession,
    session_store: BrowserSessionStore,
    *,
    authorization: AdministrativeAuthorization,
    account_id: uuid.UUID,
    password: str,
    source_network_fingerprint: str | None = None,
    user_agent_fingerprint: str | None = None,
    settings: Settings | None = None,
    now: datetime | None = None,
) -> OperatorAccount:
    configured = settings or get_settings()
    event_time = now or _utcnow()
    actor = await _require_active_administrator(session, authorization)
    account = await _load_managed_operator(session, account_id)
    if account.status != "disabled":
        raise AccountStateError("only a disabled Operator can be re-enabled")
    validate_user_chosen_password(
        password,
        username=account.username_normalized,
        display_name=account.display_name,
        current_password_hash=account.password_hash,
    )
    before_version = account.auth_version
    revoked = await session_store.revoke_all_sessions(account.account_id)
    account.status = "active"
    account.password_hash = hash_password(password)
    account.must_change_password = False
    account.temporary_password_expires_at = None
    account.password_changed_at = event_time
    account.auth_version += 1
    account.updated_at = event_time
    await session.flush()
    await append_operator_audit_event(
        session,
        category="security",
        actor_kind="human",
        actor_account_id=actor.account_id,
        actor_display_name=actor.display_name,
        effective_role=actor.role,
        request_id=authorization.authorization_reference.strip(),
        action="operator_account.reactivated_with_password_reset",
        target_type="operator_account",
        target_id=str(account.account_id),
        reason_code="operator_browser",
        outcome="succeeded",
        metadata=_browser_audit_metadata(
            authorization,
            before_status="disabled",
            after_status=account.status,
            before_auth_version=before_version,
            after_auth_version=account.auth_version,
            revoked_session_count=len(revoked),
        ),
        source_network_fingerprint=source_network_fingerprint,
        user_agent_fingerprint=user_agent_fingerprint,
        occurred_at=event_time,
        settings=configured,
    )
    return account
