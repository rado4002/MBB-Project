from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError
from sqlalchemy import CheckConstraint

from app.config import Settings
from app.database import Base
from app.models.admin_audit_log import AdminAuditLog
from app.models.operator_account import (
    OperatorAccount,
    normalize_display_name,
    normalize_email,
    normalize_username,
)
from app.operator_identity.audit import (
    _validate_metadata,
    retention_deadline,
    security_metadata_retention_deadline,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Operator.One", "operator.one"),
        ("  admin_user  ", "admin_user"),
        ("Ａbc", "abc"),
        ("a-b", "a-b"),
    ],
)
def test_username_normalization(raw: str, expected: str) -> None:
    assert normalize_username(raw) == expected


@pytest.mark.parametrize(
    "value",
    ["ab", "1operator", "operator space", "operator@mbb", "a" * 33, "équipe"],
)
def test_invalid_username_patterns(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_username(value)


def test_display_name_normalizes_and_rejects_unsafe_characters() -> None:
    assert normalize_display_name("  Jose\u0301  ") == "José"
    for invalid in ("", "A\nB", "Admin\u202eName", "x" * 101):
        with pytest.raises(ValueError):
            normalize_display_name(invalid)


def test_optional_email_normalization() -> None:
    assert normalize_email(None) is None
    assert normalize_email("   ") is None
    assert normalize_email(" Operator@Example.COM ") == "operator@example.com"
    with pytest.raises(ValueError):
        normalize_email("invalid")


def test_operator_account_applies_normalizers_without_exposing_hash() -> None:
    account = OperatorAccount(
        username_normalized=" Operator.One ",
        display_name=" Jose\u0301 ",
        email_normalized=" USER@EXAMPLE.COM ",
        password_hash="sensitive-hash",
        role="operator",
        status="active",
        auth_version=1,
        must_change_password=True,
        temporary_password_expires_at=datetime.now(timezone.utc),
    )
    assert account.username_normalized == "operator.one"
    assert account.display_name == "José"
    assert account.email_normalized == "user@example.com"
    assert "sensitive-hash" not in repr(account)


def test_operator_models_are_registered_and_legacy_audit_is_unchanged() -> None:
    assert "mbb.operator_accounts" in Base.metadata.tables
    assert "mbb.operator_audit_events" in Base.metadata.tables
    assert "mbb.operator_audit_security_metadata" in Base.metadata.tables
    legacy = Base.metadata.tables["mbb.admin_audit_log"]
    assert legacy is AdminAuditLog.__table__
    assert "actor_account_id" not in legacy.c


def test_account_constraints_and_partial_email_uniqueness_are_declared() -> None:
    table = OperatorAccount.__table__
    checks = {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    }
    assert {
        "chk_operator_accounts_username_lowercase",
        "chk_operator_accounts_username_format",
        "chk_operator_accounts_display_name_length",
        "chk_operator_accounts_email_normalized",
        "chk_operator_accounts_role",
        "chk_operator_accounts_status",
        "chk_operator_accounts_auth_version_positive",
        "chk_operator_accounts_temporary_password_consistency",
    } <= checks
    assert "uq_operator_accounts_username_normalized" in {
        constraint.name for constraint in table.constraints
    }
    email_index = next(
        index
        for index in table.indexes
        if index.name == "uq_operator_accounts_email_normalized"
    )
    assert email_index.unique is True
    assert email_index.dialect_options["postgresql"]["where"] is not None


def test_audit_retention_and_sensitive_metadata_boundary() -> None:
    settings = Settings(
        operator_audit_retention_days=365,
        operator_security_metadata_retention_days=90,
    )
    occurred_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert (retention_deadline("security", occurred_at=occurred_at, settings=settings) - occurred_at).days == 365
    assert (
        security_metadata_retention_deadline(
            occurred_at=occurred_at, settings=settings
        )
        - occurred_at
    ).days == 90
    _validate_metadata({"created_role": "operator"})
    for forbidden in (
        "password",
        "password_hash",
        "session_token",
        "csrf",
        "idempotency_key",
        "phone_number",
        "message_body",
        "provider_error",
        "ip_address",
        "user_agent",
    ):
        with pytest.raises(ValueError):
            _validate_metadata({forbidden: "sensitive"})


def test_browser_auth_configuration_defaults_off_and_db_four() -> None:
    settings = Settings()
    assert settings.browser_auth_enabled is False
    assert settings.browser_session_redis_db == 4
    assert settings.browser_session_idle_seconds == 1800
    assert settings.browser_session_absolute_seconds == 28800
    assert settings.browser_recent_reauth_seconds == 600
    assert settings.browser_max_sessions_per_account == 2
    unsafe_overrides = (
        {"browser_session_redis_db": 0},
        {"browser_session_idle_seconds": 1801},
        {"browser_session_absolute_seconds": 28801},
        {"browser_recent_reauth_seconds": 601},
        {"browser_max_sessions_per_account": 3},
        {"operator_audit_retention_days": 364},
        {"operator_security_metadata_retention_days": 91},
        {"temporary_password_lifetime_seconds": 86401},
    )
    for override in unsafe_overrides:
        with pytest.raises(ValidationError):
            Settings(**override)
