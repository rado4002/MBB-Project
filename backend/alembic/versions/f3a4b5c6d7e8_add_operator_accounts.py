"""add operator accounts

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-07-28

Downgrade is intended only before real operator data exists. Once accounts are
provisioned, dropping this table is destructive and requires separate review.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, None] = "e2f3a4b5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_accounts",
        sa.Column(
            "account_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("username_normalized", sa.String(32), nullable=False),
        sa.Column("display_name", sa.String(100), nullable=False),
        sa.Column("email_normalized", sa.String(320), nullable=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column(
            "auth_version", sa.Integer(), nullable=False, server_default=sa.text("1")
        ),
        sa.Column(
            "must_change_password",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("FALSE"),
        ),
        sa.Column(
            "temporary_password_expires_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "password_changed_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("last_login_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
        sa.PrimaryKeyConstraint("account_id", name="pk_operator_accounts"),
        sa.UniqueConstraint(
            "username_normalized", name="uq_operator_accounts_username_normalized"
        ),
        sa.CheckConstraint(
            "username_normalized = lower(username_normalized)",
            name="chk_operator_accounts_username_lowercase",
        ),
        sa.CheckConstraint(
            r"username_normalized ~ '^[a-z][a-z0-9._-]{2,31}$'",
            name="chk_operator_accounts_username_format",
        ),
        sa.CheckConstraint(
            "char_length(btrim(display_name)) BETWEEN 1 AND 100",
            name="chk_operator_accounts_display_name_length",
        ),
        sa.CheckConstraint(
            "email_normalized IS NULL OR email_normalized = lower(btrim(email_normalized))",
            name="chk_operator_accounts_email_normalized",
        ),
        sa.CheckConstraint(
            "role IN ('administrator', 'operator', 'analyst')",
            name="chk_operator_accounts_role",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'disabled')",
            name="chk_operator_accounts_status",
        ),
        sa.CheckConstraint(
            "auth_version > 0", name="chk_operator_accounts_auth_version_positive"
        ),
        sa.CheckConstraint(
            "NOT must_change_password OR temporary_password_expires_at IS NOT NULL",
            name="chk_operator_accounts_temporary_password_consistency",
        ),
        schema="mbb",
    )
    op.create_index(
        "uq_operator_accounts_email_normalized",
        "operator_accounts",
        ["email_normalized"],
        unique=True,
        schema="mbb",
        postgresql_where=sa.text("email_normalized IS NOT NULL"),
    )
    op.create_index(
        "idx_operator_accounts_status_role",
        "operator_accounts",
        ["status", "role"],
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_index(
        "idx_operator_accounts_status_role",
        table_name="operator_accounts",
        schema="mbb",
    )
    op.drop_index(
        "uq_operator_accounts_email_normalized",
        table_name="operator_accounts",
        schema="mbb",
    )
    op.drop_table("operator_accounts", schema="mbb")
