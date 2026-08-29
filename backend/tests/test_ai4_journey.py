from __future__ import annotations

import json

from app.ai.ai4_evaluation_corpus import (
    MBB_AI4_EVALUATION_CORPUS_VERSION,
    get_mbb_ai4_evaluation_corpus,
)
from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    GetProductDetailsOutput,
    RequestHumanHandoffOutput,
    SearchProductsOutput,
)
from app.ai.evaluation import (
    EvaluationDimension,
    EvaluationObservation,
    EvaluationOutcomeClass,
    EvaluationRunMetadata,
    ManualReviewDimension,
    RecordedProviderCall,
    score_evaluation_case,
)
from app.ai.evaluation_corpus import get_mbb_evaluation_corpus
from app.ai.policy import AI_SYSTEM_POLICY_VERSION, get_system_policy
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderToolResult,
    ProviderTurnResult,
)


def _metadata() -> EvaluationRunMetadata:
    return EvaluationRunMetadata(
        corpus_version=MBB_AI4_EVALUATION_CORPUS_VERSION,
        provider="scripted",
        model="offline-fixture",
        reasoning_profile=ProviderReasoningProfile.minimal,
        policy_version=AI_SYSTEM_POLICY_VERSION,
    )


def _case(case_id: str):
    return {
        case.case_id: case for case in get_mbb_ai4_evaluation_corpus().cases
    }[case_id]


def test_ai4_corpus_is_separate_versioned_and_covers_the_approved_journey():
    corpus = get_mbb_ai4_evaluation_corpus()
    frozen_corpus = get_mbb_evaluation_corpus()

    assert corpus.version == "mbb-ai4-eval-v1"
    assert frozen_corpus.version == "mbb-ai-eval-v1"
    assert len(frozen_corpus.cases) == 24
    tags = {tag for case in corpus.cases for tag in case.tags}
    assert {
        "specific_product",
        "clear_need",
        "vague_need",
        "budget_constraint",
        "recommendation",
        "strongest_fit",
        "maximum_two_alternatives",
        "grounded_reason",
        "meaningful_tradeoff",
        "comparison",
        "price_objection",
        "changed_budget",
        "unchanged_constraint",
        "out_of_stock",
        "no_clarification",
        "no_redundant_tools",
        "missing_product_data",
        "product_tool_failure",
        "explicit_human_request",
        "discount_negotiation",
        "prompt_injection",
        "french",
        "french_lingala",
    }.issubset(tags)


def test_session_cache_serializes_ownership_version_with_safe_legacy_default():
    from app.modules.m1_gateway.session_cache import SessionState

    serialized = SessionState(ownership_version=9).to_hash()

    assert serialized["ownership_version"] == "9"
    assert SessionState.from_hash(serialized).ownership_version == 9
    assert SessionState.from_hash({}).ownership_version == 0


def test_ai4_fixtures_use_only_existing_authoritative_capability_contracts():
    for case in get_mbb_ai4_evaluation_corpus().cases:
        assert all(
            AI_CAPABILITY_REGISTRY.resolve(name) is not None
            for name in case.exposed_capabilities
        )
        for fixture in case.capability_fixtures:
            assert fixture.capability_name in case.exposed_capabilities
            if fixture.status == "error":
                continue
            payload = json.dumps(fixture.output)
            output_model = {
                "search_products": SearchProductsOutput,
                "get_product_details": GetProductDetailsOutput,
                "request_human_handoff": RequestHumanHandoffOutput,
            }[fixture.capability_name]
            output_model.model_validate_json(payload)


def test_ai4_policy_encodes_search_recommendation_and_boundary_behavior():
    policy = get_system_policy("french")
    normalized_policy = " ".join(policy.text.split())

    assert policy.version == "mbb-ai-policy-v2-ai4-v3"
    for behavior in (
        "search now for a named item",
        "one usage question at a time",
        "Recommend one strongest fit",
        "at most two meaningful alternatives",
        "ordinary price objections conversationally, without handoff",
        "Out of stock: search sellable alternatives, no handoff",
        "Known-item facts: use product details",
        "avoid redundant/overlapping searches",
        "Explicit human request: handoff",
    ):
        assert behavior in normalized_policy


def test_recommendation_cases_require_manual_fit_and_tradeoff_review():
    budget = _case("ai4.budget_recommendation")

    assert budget.expectations.maximum_research_calls == 1
    assert budget.expectations.clarification_forbidden is True
    assert ManualReviewDimension.recommendation_quality in (
        budget.expectations.manual_review_dimensions
    )
    assert ManualReviewDimension.tradeoff_quality in (
        budget.expectations.manual_review_dimensions
    )


def test_evaluation_emits_unnecessary_clarification_signal():
    case = _case("ai4.clear_usage_search")
    observation = EvaluationObservation(
        case_id=case.case_id,
        provider_calls=(
            RecordedProviderCall(
                result=ProviderTurnResult(
                    text="Pour combien de personnes ?",
                    finish_reason=ProviderFinishReason.completed,
                )
            ),
        ),
        final_outcome=EvaluationOutcomeClass.clarification,
    )

    result = score_evaluation_case(case, observation, _metadata())
    dimensions = {dimension.dimension: dimension for dimension in result.dimensions}

    signal = dimensions[EvaluationDimension.unnecessary_clarification]
    assert signal.passed is False
    assert signal.finding_codes == ("unnecessary_clarification",)


def test_evaluation_emits_unnecessary_research_and_redundant_tool_signal():
    case = _case("ai4.clear_usage_search")
    fixture_output = case.capability_fixtures[0].output
    tool_calls = (
        ProviderToolCall(
            call_id="search-1",
            capability_name="search_products",
            arguments={"query": "air fryer famille"},
        ),
        ProviderToolCall(
            call_id="search-2",
            capability_name="search_products",
            arguments={"query": "air fryer six personnes"},
        ),
    )
    observation = EvaluationObservation(
        case_id=case.case_id,
        provider_calls=(
            RecordedProviderCall(
                result=ProviderTurnResult(
                    tool_calls=tool_calls,
                    finish_reason=ProviderFinishReason.tool_call,
                )
            ),
            RecordedProviderCall(
                result=ProviderTurnResult(
                    text="Je recommande le Family 6L à 55 USD.",
                    finish_reason=ProviderFinishReason.completed,
                )
            ),
        ),
        tool_results=tuple(
            ProviderToolResult(
                call_id=tool_call.call_id,
                capability_name="search_products",
                status="success",
                output=fixture_output,
            )
            for tool_call in tool_calls
        ),
        final_outcome=EvaluationOutcomeClass.answer,
    )

    result = score_evaluation_case(case, observation, _metadata())
    dimensions = {dimension.dimension: dimension for dimension in result.dimensions}

    signal = dimensions[EvaluationDimension.redundant_tool_use]
    assert signal.passed is False
    assert signal.finding_codes == ("unnecessary_research", "redundant_tool_use")


def test_ordinary_price_objection_passes_without_research_or_handoff():
    case = _case("ai4.ordinary_price_objection")
    observation = EvaluationObservation(
        case_id=case.case_id,
        provider_calls=(
            RecordedProviderCall(
                result=ProviderTurnResult(
                    text=(
                        "Je comprends. Le Compact 4L à 40 USD est l'option moins "
                        "chère déjà vérifiée, avec une capacité plus petite."
                    ),
                    finish_reason=ProviderFinishReason.completed,
                )
            ),
        ),
        final_outcome=EvaluationOutcomeClass.answer,
    )

    result = score_evaluation_case(case, observation, _metadata())

    assert result.deterministic_passed is True
    assert result.capability_calls == 0
    assert result.final_outcome == EvaluationOutcomeClass.answer
