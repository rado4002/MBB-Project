from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.config import Settings
from app.models.operator_account import OperatorAccount
from app.models.operator_audit import OperatorAuditEvent
from app.operator_identity.accounts import (
    AccountStateError,
    AdministrativeAuthorization,
    BootstrapUnavailable,
    bootstrap_first_administrator,
    create_browser_managed_operator,
    disable_browser_managed_operator,
    disable_operator_account,
    enable_browser_managed_operator,
    ManagedOperatorTargetDenied,
    OperatorAccountConflict,
    provision_operator_account,
    reactivate_operator_account,
    reset_operator_password,
    set_browser_managed_operator_password,
)
from app.operator_identity.passwords import verify_password

pytestmark = pytest.mark.skipif(
    not os.environ.get("D1_TEST_DATABASE_URL"),
    reason="requires an explicitly configured disposable D1 PostgreSQL database",
)


@pytest_asyncio.fixture(loop_scope="function")
async def db_session():
    engine = create_async_engine(os.environ["D1_TEST_DATABASE_URL"])
    async with engine.connect() as connection:
        transaction = await connection.begin()
        session = AsyncSession(bind=connection, expire_on_commit=False)
        try:
            yield session
        finally:
            await session.close()
            await transaction.rollback()
    await engine.dispose()


def _settings() -> Settings:
    return Settings(
        temporary_password_lifetime_seconds=86400,
        operator_audit_retention_days=365,
        operator_security_metadata_retention_days=90,
        browser_session_hmac_secret="s" * 32,
    )


async def _bootstrap(db_session: AsyncSession):
    return await bootstrap_first_administrator(
        db_session,
        username="first.admin",
        display_name="First Administrator",
        email=None,
        human_operator_label="Local owner console",
        settings=_settings(),
        now=datetime(2026, 7, 28, tzinfo=timezone.utc),
    )


def _authorization(account: OperatorAccount) -> AdministrativeAuthorization:
    return AdministrativeAuthorization(
        actor_account_id=account.account_id,
        authorization_reference="change-record-2026-001",
        reason="Approved local operator administration",
    )


@pytest.mark.asyncio
async def test_bootstrap_is_once_only_audited_and_never_stores_plaintext(
    db_session: AsyncSession,
) -> None:
    issued = await _bootstrap(db_session)
    plaintext = issued.consume()
    with pytest.raises(RuntimeError):
        issued.consume()
    assert plaintext not in repr(issued)
    assert verify_password(issued.account.password_hash, plaintext)
    assert plaintext not in issued.account.password_hash
    assert issued.account.must_change_password is True
    lifetime = (
        issued.account.temporary_password_expires_at
        - datetime(2026, 7, 28, tzinfo=timezone.utc)
    )
    assert lifetime.total_seconds() == 86400
    assert (
        await db_session.scalar(select(func.count()).select_from(OperatorAuditEvent))
        == 1
    )
    audit = await db_session.scalar(select(OperatorAuditEvent))
    assert audit.action == "operator_account.bootstrap_created"
    assert audit.actor_kind == "bootstrap"
    assert plaintext not in str(audit.event_metadata)
    with pytest.raises(BootstrapUnavailable):
        await _bootstrap(db_session)


@pytest.mark.asyncio
async def test_provision_reset_disable_and_reactivate_lifecycle(
    db_session: AsyncSession,
) -> None:
    administrator = (await _bootstrap(db_session)).account
    authorization = _authorization(administrator)
    provisioned = await provision_operator_account(
        db_session,
        authorization=authorization,
        username="Operator.One",
        display_name="Operator One",
        email=" OPERATOR@EXAMPLE.COM ",
        role="operator",
        settings=_settings(),
    )
    initial_password = provisioned.consume()
    account = provisioned.account
    assert account.username_normalized == "operator.one"
    assert account.email_normalized == "operator@example.com"
    assert verify_password(account.password_hash, initial_password)

    session_store = AsyncMock()
    session_store.revoke_all_sessions.return_value = ("server-only-ref",)
    previous_version = account.auth_version
    reset = await reset_operator_password(
        db_session,
        session_store,
        authorization=authorization,
        username=account.username_normalized,
        settings=_settings(),
    )
    reset_password = reset.consume()
    assert account.auth_version == previous_version + 1
    assert verify_password(account.password_hash, reset_password)
    assert not verify_password(account.password_hash, initial_password)
    assert account.status == "active"

    await disable_operator_account(
        db_session,
        session_store,
        authorization=authorization,
        username=account.username_normalized,
        settings=_settings(),
    )
    assert account.status == "disabled"
    disabled_version = account.auth_version

    disabled_reset = await reset_operator_password(
        db_session,
        session_store,
        authorization=authorization,
        username=account.username_normalized,
        settings=_settings(),
    )
    disabled_reset.consume()
    assert account.status == "disabled"
    assert account.auth_version == disabled_version + 1

    reactivated = await reactivate_operator_account(
        db_session,
        session_store,
        authorization=authorization,
        username=account.username_normalized,
        settings=_settings(),
    )
    reactivation_password = reactivated.consume()
    assert account.status == "active"
    assert account.must_change_password is True
    assert verify_password(account.password_hash, reactivation_password)
    assert session_store.revoke_all_sessions.await_count == 4

    with pytest.raises(AccountStateError):
        await reactivate_operator_account(
            db_session,
            session_store,
            authorization=authorization,
            username=account.username_normalized,
            settings=_settings(),
        )

    actions = set(
        (
            await db_session.scalars(
                select(OperatorAuditEvent.action).order_by(
                    OperatorAuditEvent.occurred_at
                )
            )
        ).all()
    )
    assert {
        "operator_account.bootstrap_created",
        "operator_account.provisioned",
        "operator_account.password_reset",
        "operator_account.disabled",
        "operator_account.reactivated_with_password_reset",
    } <= actions


@pytest.mark.asyncio
async def test_database_uniqueness_and_account_checks(db_session: AsyncSession) -> None:
    await db_session.execute(
        text(
            """
            INSERT INTO mbb.operator_accounts
                (username_normalized, display_name, email_normalized, password_hash,
                 role, status, auth_version, must_change_password)
            VALUES
                ('valid.one', 'Valid One', 'one@example.com', 'hash',
                 'operator', 'active', 1, FALSE),
                ('valid.two', 'Valid Two', NULL, 'hash',
                 'analyst', 'disabled', 1, FALSE),
                ('valid.three', 'Valid Three', NULL, 'hash',
                 'administrator', 'active', 1, FALSE)
            """
        )
    )

    invalid_statements = [
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, email_normalized, password_hash,
             role, status, auth_version, must_change_password)
            VALUES ('valid.one', 'Duplicate Username', 'other@example.com', 'hash',
                    'operator', 'active', 1, FALSE)""",
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, password_hash, role, status,
             auth_version, must_change_password)
            VALUES ('Valid.Four', 'Name', 'hash', 'operator', 'active', 1, FALSE)""",
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, email_normalized, password_hash,
             role, status, auth_version, must_change_password)
            VALUES ('valid.four', 'Name', 'one@example.com', 'hash',
                    'operator', 'active', 1, FALSE)""",
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, password_hash, role, status,
             auth_version, must_change_password)
            VALUES ('valid.five', 'Name', 'hash', 'owner', 'active', 1, FALSE)""",
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, password_hash, role, status,
             auth_version, must_change_password)
            VALUES ('valid.six', 'Name', 'hash', 'operator', 'locked', 1, FALSE)""",
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, password_hash, role, status,
             auth_version, must_change_password)
            VALUES ('valid.seven', 'Name', 'hash', 'operator', 'active', 0, FALSE)""",
        """INSERT INTO mbb.operator_accounts
            (username_normalized, display_name, password_hash, role, status,
             auth_version, must_change_password)
            VALUES ('valid.eight', 'Name', 'hash', 'operator', 'active', 1, TRUE)""",
    ]
    for statement in invalid_statements:
        savepoint = await db_session.begin_nested()
        with pytest.raises(Exception):
            await db_session.execute(text(statement))
        await savepoint.rollback()


def test_cli_exposes_only_approved_internal_commands() -> None:
    import app.operator_identity.accounts as account_services
    from scripts.operator_accounts import build_parser

    assert not hasattr(account_services, "delete_operator_account")
    parser = build_parser()
    help_text = parser.format_help()
    for command in (
        "bootstrap-first-administrator",
        "provision-operator-account",
        "reset-operator-password",
        "disable-operator-account",
        "reactivate-operator-account",
    ):
        assert command in help_text


@pytest.mark.asyncio
async def test_browser_managed_operator_lifecycle_uses_explicit_passwords(
    db_session: AsyncSession,
) -> None:
    administrator = (await _bootstrap(db_session)).account
    authorization = _authorization(administrator)
    initial_password = "Cobalt-River-83!"
    account = await create_browser_managed_operator(
        db_session,
        authorization=authorization,
        username="browser.operator",
        display_name="Browser Operator",
        email="browser.operator@example.test",
        password=initial_password,
        settings=_settings(),
        source_network_fingerprint="a" * 64,
        user_agent_fingerprint="b" * 64,
    )
    assert account.role == "operator"
    assert account.status == "active"
    assert account.must_change_password is False
    assert account.temporary_password_expires_at is None
    assert verify_password(account.password_hash, initial_password)

    session_store = AsyncMock()
    session_store.revoke_all_sessions.return_value = ("one", "two")
    initial_version = account.auth_version
    new_password = "Sunset-Lantern-74!"
    await set_browser_managed_operator_password(
        db_session,
        session_store,
        authorization=authorization,
        account_id=account.account_id,
        password=new_password,
        settings=_settings(),
    )
    assert account.auth_version == initial_version + 1
    assert not verify_password(account.password_hash, initial_password)
    assert verify_password(account.password_hash, new_password)
    assert account.must_change_password is False

    await disable_browser_managed_operator(
        db_session,
        session_store,
        authorization=authorization,
        account_id=account.account_id,
        settings=_settings(),
    )
    assert account.status == "disabled"
    disabled_version = account.auth_version

    enabled_password = "Marble-Window-61!"
    await enable_browser_managed_operator(
        db_session,
        session_store,
        authorization=authorization,
        account_id=account.account_id,
        password=enabled_password,
        settings=_settings(),
    )
    assert account.status == "active"
    assert account.auth_version == disabled_version + 1
    assert account.must_change_password is False
    assert verify_password(account.password_hash, enabled_password)
    assert session_store.revoke_all_sessions.await_count == 3

    audit_events = (
        await db_session.scalars(
            select(OperatorAuditEvent).where(
                OperatorAuditEvent.target_id == str(account.account_id)
            )
        )
    ).all()
    assert [event.action for event in audit_events] == [
        "operator_account.provisioned",
        "operator_account.password_reset",
        "operator_account.disabled",
        "operator_account.reactivated_with_password_reset",
    ]
    audit_text = " ".join(str(event.event_metadata) for event in audit_events)
    for secret in (initial_password, new_password, enabled_password):
        assert secret not in audit_text
    assert all(event.reason_code == "operator_browser" for event in audit_events)


@pytest.mark.asyncio
async def test_browser_management_rejects_duplicates_and_non_operator_targets(
    db_session: AsyncSession,
) -> None:
    administrator = (await _bootstrap(db_session)).account
    authorization = _authorization(administrator)
    await create_browser_managed_operator(
        db_session,
        authorization=authorization,
        username="unique.operator",
        display_name="Unique Operator",
        email="unique@example.test",
        password="Copper-River-47!",
        settings=_settings(),
    )
    with pytest.raises(OperatorAccountConflict):
        await create_browser_managed_operator(
            db_session,
            authorization=authorization,
            username="unique.operator",
            display_name="Duplicate Operator",
            email="different@example.test",
            password="Amber-Lantern-58!",
            settings=_settings(),
        )

    session_store = AsyncMock()
    with pytest.raises(ManagedOperatorTargetDenied):
        await disable_browser_managed_operator(
            db_session,
            session_store,
            authorization=authorization,
            account_id=administrator.account_id,
            settings=_settings(),
        )
    session_store.revoke_all_sessions.assert_not_awaited()
