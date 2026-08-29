from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from app.ai.capabilities import RequestHumanHandoffInput
from app.ai.commercial_state import (
    CommercialState,
    CommercialStateProposal,
    CommercialStateUpdate,
    NextObjective,
    PurchaseIntent,
    apply_commercial_state_update,
    commercial_state_projection,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION, get_system_policy
from app.modules.m4_conversation.ai_handoff import handoff_acknowledgment


def test_normal_finalizer_can_set_considering_but_not_ready_or_post_handoff() -> None:
    proposal = CommercialStateProposal.model_validate(
        {
            "response_text": "Je comprends.",
            "state_update": {"purchase_intent": "considering"},
        }
    )
    state = apply_commercial_state_update(
        None,
        expected_revision=0,
        state_update=proposal.state_update,
    )

    assert state is not None
    assert state.purchase_intent is PurchaseIntent.considering
    with pytest.raises(ValidationError):
        CommercialStateUpdate.model_validate({"purchase_intent": "ready"})
    with pytest.raises(ValidationError):
        CommercialStateUpdate.model_validate(
            {"next_objective": "human_commercial_continuation"}
        )


@pytest.mark.parametrize(
    ("payload", "valid"),
    (
        (
            {
                "reason_category": "qualified_purchase_intent",
                "selected_sellable_item_id": str(uuid.uuid4()),
                "purchase_intent": "ready",
            },
            True,
        ),
        ({"reason_category": "qualified_purchase_intent"}, False),
        (
            {
                "reason_category": "authority_required",
                "purchase_intent": "considering",
            },
            True,
        ),
        (
            {
                "reason_category": "authority_required",
                "purchase_intent": "ready",
            },
            False,
        ),
        ({"reason_category": "explicit_human_request"}, True),
        (
            {
                "reason_category": "explicit_human_request",
                "purchase_intent": "considering",
            },
            False,
        ),
        ({"reason_category": "reliability_tool_failure"}, True),
        ({"reason_category": "customer_requested_human"}, True),
        ({"reason_category": "policy_exception"}, True),
    ),
)
def test_terminal_contract_cross_validates_reason_and_purchase_transition(
    payload: dict[str, str],
    valid: bool,
) -> None:
    if valid:
        RequestHumanHandoffInput.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            RequestHumanHandoffInput.model_validate(payload)


def test_historical_terminal_state_is_not_projected_as_fresh_handoff_evidence() -> None:
    state = CommercialState(
        purchase_intent=PurchaseIntent.ready,
        next_objective=NextObjective.human_commercial_continuation,
        selected_sellable_item_ids=[uuid.uuid4()],
    )

    projection = commercial_state_projection(state)

    assert "purchase_intent" not in projection
    assert projection["next_objective"] == "human_commercial_continuation"


@pytest.mark.parametrize(
    "reason",
    (
        "explicit_human_request",
        "authority_required",
        "reliability_tool_failure",
    ),
)
def test_server_acknowledgments_are_short_and_make_no_transaction_claim(
    reason: str,
) -> None:
    acknowledgment = handoff_acknowledgment(reason=reason, language="french")

    assert len(acknowledgment) <= 200
    lowered = acknowledgment.lower()
    for forbidden in ("commande confirm", "réserv", "paiement", "livraison promise"):
        assert forbidden not in lowered


def test_ai4e_policy_is_versioned_and_encodes_fresh_evidence_boundary() -> None:
    policy = " ".join(get_system_policy("french").text.split())

    assert AI_SYSTEM_POLICY_VERSION == "mbb-ai-policy-v2-ai4-v3"
    for required in (
        "Purchase interest is not commitment",
        "Conditional commitment remains considering",
        "exactly one resolved Sellable Item",
        "Saved ready intent or human_commercial_continuation alone never justifies",
        "Normal continuity updates may set purchase_intent only to none or considering",
    ):
        assert required in policy
