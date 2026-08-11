"""add AI handoff authority state and escalation values

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-08-11
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "a7b8c9d0e1f2"
down_revision: Union[str, None] = "f6a7b8c9d0e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "chk_conv_exclusive_owner",
        "conversations",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        "chk_conv_exclusive_owner",
        "conversations",
        "(owner_type = 'ai' AND human_owner_account_id IS NULL "
        "AND ai_execution_state IN ('eligible', 'paused')) OR "
        "(owner_type = 'human' AND human_owner_account_id IS NOT NULL "
        "AND ai_execution_state = 'paused')",
        schema="mbb",
    )

    op.drop_constraint(
        "chk_esc_reason", "escalation_tickets", schema="mbb", type_="check"
    )
    op.drop_constraint(
        "chk_esc_source", "escalation_tickets", schema="mbb", type_="check"
    )
    op.drop_constraint(
        "chk_esc_escalation_type",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        "chk_esc_reason",
        "escalation_tickets",
        "reason IN ('voice_note', 'complex_complaint', 'high_value_lead', "
        "'unresolved_3x', 'sav_issue', 'human_handoff')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_source",
        "escalation_tickets",
        "source IN ('legacy', 'operator_browser', 'ai_capability')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_escalation_type",
        "escalation_tickets",
        "escalation_type IS NULL OR escalation_type IN "
        "('voice_note', 'complex_issue', 'high_value_lead', 'payment_issue', "
        "'human_handoff')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_ai_capability_fields",
        "escalation_tickets",
        "source <> 'ai_capability' OR "
        "(escalation_type = 'human_handoff' AND reason = 'human_handoff' "
        "AND operator_reason IS NULL AND created_by_account_id IS NULL)",
        schema="mbb",
    )


def downgrade() -> None:
    op.drop_constraint(
        "chk_esc_ai_capability_fields",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_escalation_type",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_source", "escalation_tickets", schema="mbb", type_="check"
    )
    op.drop_constraint(
        "chk_esc_reason", "escalation_tickets", schema="mbb", type_="check"
    )
    op.create_check_constraint(
        "chk_esc_reason",
        "escalation_tickets",
        "reason IN ('voice_note', 'complex_complaint', 'high_value_lead', "
        "'unresolved_3x', 'sav_issue')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_source",
        "escalation_tickets",
        "source IN ('legacy', 'operator_browser')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_escalation_type",
        "escalation_tickets",
        "escalation_type IS NULL OR escalation_type IN "
        "('voice_note', 'complex_issue', 'high_value_lead', 'payment_issue')",
        schema="mbb",
    )

    op.drop_constraint(
        "chk_conv_exclusive_owner",
        "conversations",
        schema="mbb",
        type_="check",
    )
    op.create_check_constraint(
        "chk_conv_exclusive_owner",
        "conversations",
        "(owner_type = 'ai' AND human_owner_account_id IS NULL "
        "AND ai_execution_state = 'eligible') OR "
        "(owner_type = 'human' AND human_owner_account_id IS NOT NULL "
        "AND ai_execution_state = 'paused')",
        schema="mbb",
    )
