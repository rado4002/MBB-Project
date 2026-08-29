from __future__ import annotations

from app.ai.ai4_evaluation_corpus import get_mbb_ai4_evaluation_corpus
from app.ai.ai4e_evaluation_corpus import (
    MBB_AI4E_EVALUATION_CORPUS_VERSION,
    get_mbb_ai4e_evaluation_corpus,
)
from app.ai.evaluation import (
    EvaluationObservation,
    EvaluationOutcomeClass,
    EvaluationOverallResult,
    EvaluationRunMetadata,
    RecordedProviderCall,
    score_evaluation_case,
)
from app.ai.evaluation_corpus import get_mbb_evaluation_corpus
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderToolResult,
    ProviderTurnResult,
)


def _metadata() -> EvaluationRunMetadata:
    return EvaluationRunMetadata(
        corpus_version=MBB_AI4E_EVALUATION_CORPUS_VERSION,
        provider="scripted",
        model="offline-fixture",
        reasoning_profile=ProviderReasoningProfile.minimal,
        policy_version=AI_SYSTEM_POLICY_VERSION,
    )


def test_ai4e_corpus_is_separate_complete_and_leaves_frozen_corpora_intact() -> None:
    corpus = get_mbb_ai4e_evaluation_corpus()

    assert corpus.version == "mbb-ai4e-eval-v1"
    assert len(corpus.cases) == 66
    assert get_mbb_evaluation_corpus().version == "mbb-ai-eval-v1"
    assert get_mbb_ai4_evaluation_corpus().version == "mbb-ai4-eval-v1"
    assert {case.tags[0] for case in corpus.cases} == {
        "non_purchase",
        "qualified_intent",
        "conditional_intent",
        "explicit_human",
        "product_state",
        "handoff_safety",
        "return_to_ai",
        "change_of_mind",
        "acknowledgment",
        "multilingual",
        "operator",
    }
    assert [case.case_id.split(".")[1] for case in corpus.cases] == [
        f"{number:02d}" for number in range(1, 67)
    ]


def test_qualified_and_conditional_cases_encode_distinct_terminal_arguments() -> None:
    cases = {case.case_id: case for case in get_mbb_ai4e_evaluation_corpus().cases}
    qualified = cases["ai4e.10.take_6l"]
    conditional = cases["ai4e.14.discount_condition"]
    human = cases["ai4e.17.human_without_product"]

    assert qualified.expectations.capability_arguments[0].arguments[
        "purchase_intent"
    ] == "ready"
    assert conditional.expectations.capability_arguments[0].arguments == {
        "reason_category": "authority_required",
        "purchase_intent": "considering",
    }
    assert human.expectations.capability_arguments[0].arguments == {
        "reason_category": "explicit_human_request"
    }


def test_scripted_offline_non_purchase_and_qualified_observations_score() -> None:
    cases = {case.case_id: case for case in get_mbb_ai4e_evaluation_corpus().cases}
    non_purchase = cases["ai4e.04.interest_like"]
    non_purchase_result = score_evaluation_case(
        non_purchase,
        EvaluationObservation(
            case_id=non_purchase.case_id,
            provider_calls=(
                RecordedProviderCall(
                    result=ProviderTurnResult(
                        text="Oui, le 6L peut être une bonne option pour une famille.",
                        finish_reason=ProviderFinishReason.completed,
                    )
                ),
            ),
            final_outcome=EvaluationOutcomeClass.answer,
        ),
        _metadata(),
    )
    assert non_purchase_result.overall_result in {
        EvaluationOverallResult.passed,
        EvaluationOverallResult.manual_review,
    }
    assert non_purchase_result.deterministic_passed is True

    qualified = cases["ai4e.10.take_6l"]
    fixture = qualified.capability_fixtures[0]
    arguments = qualified.expectations.capability_arguments[0].arguments
    tool_call = ProviderToolCall(
        call_id="ai4e-qualified",
        capability_name="request_human_handoff",
        arguments=arguments,
    )
    qualified_result = score_evaluation_case(
        qualified,
        EvaluationObservation(
            case_id=qualified.case_id,
            provider_calls=(
                RecordedProviderCall(
                    result=ProviderTurnResult(
                        tool_calls=(tool_call,),
                        finish_reason=ProviderFinishReason.tool_call,
                    )
                ),
            ),
            tool_results=(
                ProviderToolResult(
                    call_id=tool_call.call_id,
                    capability_name=tool_call.capability_name,
                    status="success",
                    output=fixture.output,
                ),
            ),
            final_outcome=EvaluationOutcomeClass.handoff,
        ),
        _metadata(),
    )
    assert qualified_result.deterministic_passed is True
