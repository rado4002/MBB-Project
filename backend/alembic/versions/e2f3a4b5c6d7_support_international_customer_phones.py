"""support international customer phone numbers

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-20
"""
from typing import Sequence, Union

from alembic import op


revision: str = "e2f3a4b5c6d7"
down_revision: Union[str, None] = "d1e2f3a4b5c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

CONSTRAINT_NAME = "chk_phone_format"
INTERNATIONAL_E164_CHECK = r"phone_number ~ '^\+[1-9][0-9]{6,14}$'"
DRC_PHONE_CHECK = r"phone_number ~ '^\+243[0-9]{9}$'"


def upgrade() -> None:
    op.drop_constraint(
        CONSTRAINT_NAME,
        "customers",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "customers",
        INTERNATIONAL_E164_CHECK,
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_constraint(
        CONSTRAINT_NAME,
        "customers",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        CONSTRAINT_NAME,
        "customers",
        DRC_PHONE_CHECK,
        schema="mbb",
    )
