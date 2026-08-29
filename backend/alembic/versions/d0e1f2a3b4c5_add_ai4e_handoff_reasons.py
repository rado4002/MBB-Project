"""widen AI handoff reasons for AI-4E

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op


revision: str = "d0e1f2a3b4c5"
down_revision: Union[str, None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "chk_esc_ai_capability_fields",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_reason", "escalation_tickets", schema="mbb", type_="check"
    )
    op.create_check_constraint(
        "chk_esc_reason",
        "escalation_tickets",
        "reason IN ('voice_note', 'complex_complaint', 'high_value_lead', "
        "'unresolved_3x', 'sav_issue', 'human_handoff', "
        "'qualified_purchase_intent', 'explicit_human_request', "
        "'authority_required', 'reliability_tool_failure')",
        schema="mbb",
    )
    op.create_check_constraint(
        "chk_esc_ai_capability_fields",
        "escalation_tickets",
        "source <> 'ai_capability' OR "
        "(escalation_type = 'human_handoff' AND reason IN "
        "('human_handoff', 'qualified_purchase_intent', "
        "'explicit_human_request', 'authority_required', "
        "'reliability_tool_failure') "
        "AND operator_reason IS NULL AND created_by_account_id IS NULL)",
        schema="mbb",
    )


def downgrade() -> None:
    op.execute(
        "UPDATE mbb.escalation_tickets SET reason = 'human_handoff' "
        "WHERE reason IN "
        "('qualified_purchase_intent', 'explicit_human_request', "
        "'authority_required', 'reliability_tool_failure')"
    )
    op.drop_constraint(
        "chk_esc_ai_capability_fields",
        "escalation_tickets",
        schema="mbb",
        type_="check",
    )
    op.drop_constraint(
        "chk_esc_reason", "escalation_tickets", schema="mbb", type_="check"
    )
    op.create_check_constraint(
        "chk_esc_reason",
        "escalation_tickets",
        "reason IN ('voice_note', 'complex_complaint', 'high_value_lead', "
        "'unresolved_3x', 'sav_issue', 'human_handoff')",
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
