"""Operator account persistence for future individual browser authentication."""

from __future__ import annotations

import re
import unicodedata
import uuid
from datetime import datetime

from sqlalchemy import (
    TIMESTAMP,
    Boolean,
    CheckConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.database import Base

USERNAME_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{2,31}$")
_BIDI_OVERRIDE_CHARACTERS = frozenset(
    chr(codepoint)
    for codepoint in (
        0x202A,
        0x202B,
        0x202C,
        0x202D,
        0x202E,
        0x2066,
        0x2067,
        0x2068,
        0x2069,
    )
)


def normalize_username(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError("username must match ^[a-z][a-z0-9._-]{2,31}$")
    return normalized


def normalize_display_name(value: str) -> str:
    normalized = unicodedata.normalize("NFC", value).strip()
    if not 1 <= len(normalized) <= 100:
        raise ValueError("display name must contain 1 to 100 characters")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("display name must not contain control characters")
    if any(character in _BIDI_OVERRIDE_CHARACTERS for character in normalized):
        raise ValueError("display name must not contain bidirectional controls")
    return normalized


def normalize_email(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    if not normalized:
        return None
    if len(normalized) > 320 or normalized.count("@") != 1:
        raise ValueError("email is invalid")
    local, domain = normalized.split("@", maxsplit=1)
    if not local or not domain or any(character.isspace() for character in normalized):
        raise ValueError("email is invalid")
    if any(unicodedata.category(character) == "Cc" for character in normalized):
        raise ValueError("email is invalid")
    return normalized


class OperatorAccount(Base):
    __tablename__ = "operator_accounts"
    __table_args__ = (
        UniqueConstraint(
            "username_normalized", name="uq_operator_accounts_username_normalized"
        ),
        CheckConstraint(
            "username_normalized = lower(username_normalized)",
            name="chk_operator_accounts_username_lowercase",
        ),
        CheckConstraint(
            r"username_normalized ~ '^[a-z][a-z0-9._-]{2,31}$'",
            name="chk_operator_accounts_username_format",
        ),
        CheckConstraint(
            "char_length(btrim(display_name)) BETWEEN 1 AND 100",
            name="chk_operator_accounts_display_name_length",
        ),
        CheckConstraint(
            "email_normalized IS NULL OR email_normalized = lower(btrim(email_normalized))",
            name="chk_operator_accounts_email_normalized",
        ),
        CheckConstraint(
            "role IN ('administrator', 'operator', 'analyst')",
            name="chk_operator_accounts_role",
        ),
        CheckConstraint(
            "status IN ('active', 'disabled')",
            name="chk_operator_accounts_status",
        ),
        CheckConstraint(
            "auth_version > 0", name="chk_operator_accounts_auth_version_positive"
        ),
        CheckConstraint(
            "NOT must_change_password OR temporary_password_expires_at IS NOT NULL",
            name="chk_operator_accounts_temporary_password_consistency",
        ),
        Index(
            "uq_operator_accounts_email_normalized",
            "email_normalized",
            unique=True,
            postgresql_where=text("email_normalized IS NOT NULL"),
        ),
        Index("idx_operator_accounts_status_role", "status", "role"),
        {"schema": "mbb"},
    )

    account_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    username_normalized: Mapped[str] = mapped_column(String(32), nullable=False)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email_normalized: Mapped[str | None] = mapped_column(String(320), nullable=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="active"
    )
    auth_version: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("1")
    )
    must_change_password: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("FALSE")
    )
    temporary_password_expires_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        TIMESTAMP(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True), nullable=False, server_default=text("NOW()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        TIMESTAMP(timezone=True),
        nullable=False,
        server_default=text("NOW()"),
        onupdate=text("NOW()"),
    )

    @validates("username_normalized")
    def _normalize_username(self, _key: str, value: str) -> str:
        return normalize_username(value)

    @validates("display_name")
    def _normalize_display_name(self, _key: str, value: str) -> str:
        return normalize_display_name(value)

    @validates("email_normalized")
    def _normalize_email(self, _key: str, value: str | None) -> str | None:
        return normalize_email(value)

    def __repr__(self) -> str:
        return (
            f"OperatorAccount(account_id={self.account_id!r}, "
            f"username_normalized={self.username_normalized!r}, "
            f"role={self.role!r}, status={self.status!r})"
        )
