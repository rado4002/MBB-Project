from __future__ import annotations

import json

import pytest

from app.ai.commercial_evaluation import (
    COMMERCIAL_EVALUATION_VERSION,
    COMMERCIAL_LIVE_CASE_IDS,
    get_commercial_evaluation_corpus,
    score_commercial_evaluation_case,
)
from app.ai.commercial_response import commercial_response_fallback
from app.ai.evaluation import (
    EvaluationObservation,
    EvaluationOutcomeClass,
    RecordedProviderCall,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderToolResult,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from app.ai.live_evaluation import LiveEvaluationBudgetState, LiveEvaluationRunBudget, LiveEvaluationSource
from app.ai.provider_contract import ProviderReasoningProfile


def _case(case_id: str):
    return next(case for case in get_commercial_evaluation_corpus().cases if case.case_id == case_id)


def _fixture(case, capability_name: str):
    return next(
        fixture
        for fixture in case.capability_fixtures
        if fixture.capability_name == capability_name
    )


def _observation(case_id: str, plan: str) -> EvaluationObservation:
    case = _case(case_id)
    capability = case.expectations.required_capabilities[0]
    arguments = (
        case.expectations.capability_arguments[0].arguments
        if case.expectations.capability_arguments
        else {"query": "air fryer"}
    )
    call = ProviderToolCall(
        call_id="call_fixture",
        capability_name=capability,
        arguments=arguments,
    )
    fixture = _fixture(case, capability)
    result = ProviderToolResult(
        call_id=call.call_id,
        capability_name=capability,
        status="success",
        output=fixture.output,
    )
    return EvaluationObservation(
        case_id=case_id,
        provider_calls=(
            RecordedProviderCall(
                result=ProviderTurnResult(
                    tool_calls=(call,),
                    finish_reason=ProviderFinishReason.tool_call,
                ),
                latency_ms=4,
            ),
            RecordedProviderCall(
                result=ProviderTurnResult(
                    text=plan,
                    finish_reason=ProviderFinishReason.completed,
                ),
                latency_ms=5,
            ),
        ),
        tool_results=(result,),
        final_outcome=EvaluationOutcomeClass.answer,
    )


def _budget_plan(**overrides: object) -> str:
    values: dict[str, object] = {
        "response_kind": "RECOMMENDATION",
        "product_refs": ["10000000-0000-4000-8000-000000000102"],
        "fact_fields": ["NAME", "CURRENT_PRICE", "CURRENT_AVAILABILITY"],
        "recommendation_ref": "10000000-0000-4000-8000-000000000102",
        "recommendation_reason": "BUDGET_FIT",
        "next_action": "NONE",
        "clarification": "NONE",
        "tone": "WARM",
    }
    values.update(overrides)
    return json.dumps(values)


def test_commercial_evaluation_is_separately_versioned_and_bounded() -> None:
    corpus = get_commercial_evaluation_corpus()

    assert corpus.version == COMMERCIAL_EVALUATION_VERSION
    assert tuple(case.case_id for case in corpus.cases) == COMMERCIAL_LIVE_CASE_IDS
    assert COMMERCIAL_EVALUATION_VERSION != "mbb-ai-eval-v1"


def test_valid_recommendation_is_grounded_and_rendered_from_authority() -> None:
    case = _case("product.discovery.budget_usd")
    result = score_commercial_evaluation_case(
        case,
        _observation(case.case_id, _budget_plan()),
    )

    assert result.deterministic_passed is True
    assert result.plan_valid is True
    assert result.grounding_passed is True
    assert result.final_text is not None
    assert "55.00 USD" in result.final_text
    assert result.raw_provider_text not in result.final_text
    assert result.raw_text_bypass is False


@pytest.mark.parametrize(
    "raw_plan",
    [
        "{malformed",
        "```json\n" + _budget_plan() + "\n```",
        "Voici le plan: " + _budget_plan(),
        _budget_plan(price="1", available=True),
        _budget_plan(product_refs=["10000000-0000-4000-8000-000000000999"]),
        _budget_plan(recommendation_reason="CHEAPEST"),
        _budget_plan(next_action="RESERVE"),
        _budget_plan(order_state="PAID"),
        _budget_plan(delivery="TOMORROW"),
        _budget_plan(future_availability=True),
        _budget_plan(response_kind=None),
    ],
)
def test_invalid_or_unsupported_plans_fail_closed_without_raw_text(raw_plan: str) -> None:
    case = _case("product.discovery.budget_usd")
    result = score_commercial_evaluation_case(
        case,
        _observation(case.case_id, raw_plan),
    )

    assert result.deterministic_passed is False
    assert result.plan_valid is False
    assert result.fallback_used is True
    assert result.final_text == commercial_response_fallback("french")
    assert result.final_text != raw_plan
    assert result.raw_text_bypass is False


def test_lingala_plan_is_valid_but_uses_native_review_fallback() -> None:
    case = _case("language.french_lingala")
    plan = _budget_plan(recommendation_reason="BUDGET_FIT", next_action="SEARCH_MORE")
    result = score_commercial_evaluation_case(case, _observation(case.case_id, plan))

    assert result.deterministic_passed is True
    assert result.plan_valid is True
    assert result.fallback_used is True
    assert result.fallback_code == "commercial_plan_language_review_required"
    assert result.native_review_required is True
    assert result.final_text == commercial_response_fallback("lingala")
    assert result.raw_text_bypass is False


def test_terminal_handoff_remains_one_call_with_null_text() -> None:
    case = _case("handoff.explicit_human")
    call = ProviderToolCall(
        call_id="call_handoff",
        capability_name="request_human_handoff",
        arguments={"reason": "customer_requested_human"},
    )
    fixture = _fixture(case, "request_human_handoff")
    observation = EvaluationObservation(
        case_id=case.case_id,
        provider_calls=(
            RecordedProviderCall(
                result=ProviderTurnResult(
                    tool_calls=(call,),
                    finish_reason=ProviderFinishReason.tool_call,
                )
            ),
        ),
        tool_results=(
            ProviderToolResult(
                call_id=call.call_id,
                capability_name=call.capability_name,
                status="success",
                output=fixture.output,
            ),
        ),
        final_outcome=EvaluationOutcomeClass.handoff,
    )

    result = score_commercial_evaluation_case(case, observation)

    assert result.deterministic_passed is True
    assert result.handoff_correct is True
    assert result.final_text is None
    assert result.provider_calls == 1
    assert result.capability_executions == 1


@pytest.mark.asyncio
async def test_live_source_uses_structured_policy_and_scores_rendered_output() -> None:
    case = _case("product.discovery.budget_usd")
    observation = _observation(case.case_id, _budget_plan())

    class ScriptedAdapter:
        def __init__(self) -> None:
            self.results = [call.result for call in observation.provider_calls]
            self.requests: list[ProviderTurnRequest] = []

        async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
            self.requests.append(request)
            return self.results.pop(0)

    adapter = ScriptedAdapter()
    source = LiveEvaluationSource(
        adapter,  # type: ignore[arg-type]
        reasoning_profile=ProviderReasoningProfile.default,
        budget_state=LiveEvaluationBudgetState(LiveEvaluationRunBudget()),
        structured_commercial=True,
    )

    result = score_commercial_evaluation_case(case, await source.observe(case))

    assert len(adapter.requests) == 2
    assert "output exactly one JSON object" in adapter.requests[0].system_instruction
    assert result.deterministic_passed is True
    assert result.final_text != result.raw_provider_text
