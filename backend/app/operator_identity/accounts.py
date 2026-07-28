"""Transactional internal services for operator-account lifecycle commands."""

from __future__ import annotations

import base64
import secrets
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.models.operator_account import (
    OperatorAccount,
    normalize_display_name,
    normalize_username,
)
from app.operator_identity.audit import append_operator_audit_event
from app.operator_identity.browser_sessions import BrowserSessionStore
from app.operator_identity.passwords import hash_password

ALLOWED_ROLES = frozenset({"administrator", "operator", "analyst"})


class OperatorAccountError(RuntimeError):
    pass


class BootstrapUnavailable(OperatorAccountError):
    pass


class AdministrativeAuthorizationDenied(OperatorAccountError):
    pass


class AccountStateError(OperatorAccountError):
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
