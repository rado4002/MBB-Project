from __future__ import annotations

import argparse
import asyncio
import json

import pytest

from app.adapters.base import ProviderTurnAdapter
from app.ai.evaluation_corpus import get_mbb_evaluation_corpus
from app.ai.journey_harness import ProductionJourneyObservationSource, ScriptedProvider
from app.ai.live_evaluation import (
    LiveEvaluationBudgetExceeded,
    LiveEvaluationConfigurationError,
    LiveEvaluationProviderFailure,
    LiveEvaluationRunBudget,
    LiveJourneyController,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from app.config import Settings
from scripts import run_ai_evaluation


def _args(**overrides) -> argparse.Namespace:
    values = {
        "replay": None,
        "live": True,
        "profiles": ["standard"],
        "case_ids": None,
        "output": None,
        "pretty": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def _source_factory(budget: LiveEvaluationRunBudget):
    return lambda provider, profile: ProductionJourneyObservationSource(
        provider,
        case_service_factory=lambda bounded, case: run_ai_evaluation._case_service(
            bounded,
            case,
            budget,
        ),
        reasoning_profile=profile,
    )


def _controller(
    adapter: ProviderTurnAdapter,
    *,
    budget: LiveEvaluationRunBudget | None = None,
    authorized: bool = True,
) -> LiveJourneyController:
    configured = budget or LiveEvaluationRunBudget()
    return LiveJourneyController(
        adapter,
        source_factory=_source_factory(configured),
        policy_version="test-policy",
        budget=configured,
        explicitly_authorized=authorized,
    )


def _product_search_results() -> list[ProviderTurnResult]:
    return [
        ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="search_1",
                    capability_name="search_products",
                    arguments={"query": "air fryer"},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
        ),
        ProviderTurnResult(
            text="J’ai trouvé un modèle disponible.",
            finish_reason=ProviderFinishReason.completed,
        ),
    ]


@pytest.mark.asyncio
async def test_live_controller_observes_the_production_journey_path():
    adapter = ScriptedProvider(_product_search_results())
    controller = _controller(adapter)

    report = await controller.run(
        get_mbb_evaluation_corpus(),
        case_ids=("product.discovery.normal",),
        reasoning_profiles=(ProviderReasoningProfile.standard,),
    )

    assert report.case_ids == ("product.discovery.normal",)
    assert report.reports[0].aggregate.cases_executed == 1
    assert report.reports[0].aggregate.provider_calls == 2
    assert len(adapter.requests) == 2
    assert adapter.requests[0].reasoning_profile == ProviderReasoningProfile.standard
    assert adapter.requests[1].messages[-1].role == "tool_result"


def test_live_controller_requires_explicit_authorization():
    with pytest.raises(
        LiveEvaluationConfigurationError,
        match="live_authorization_required",
    ):
        _controller(ScriptedProvider([]), authorized=False)


class _IdentitylessProvider(ProviderTurnAdapter):
    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        raise AssertionError("provider must not be called")


def test_live_controller_requires_provider_and_model_identity():
    with pytest.raises(
        LiveEvaluationConfigurationError,
        match="provider_identity_unavailable",
    ):
        _controller(_IdentitylessProvider())


@pytest.mark.asyncio
async def test_live_controller_stops_before_exceeding_aggregate_provider_budget():
    budget = LiveEvaluationRunBudget(max_total_provider_calls=1)
    controller = _controller(
        ScriptedProvider(_product_search_results()),
        budget=budget,
    )

    with pytest.raises(
        LiveEvaluationBudgetExceeded,
        match="total_provider_calls",
    ) as captured:
        await controller.run(
            get_mbb_evaluation_corpus(),
            case_ids=("product.discovery.normal",),
            reasoning_profiles=(ProviderReasoningProfile.standard,),
        )

    assert captured.value.evidence.completed_provider_calls == 1


@pytest.mark.asyncio
async def test_live_controller_reserves_token_spend_before_transport():
    budget = LiveEvaluationRunBudget(max_total_reserved_tokens=1)
    adapter = ScriptedProvider(_product_search_results())
    controller = _controller(adapter, budget=budget)

    with pytest.raises(
        LiveEvaluationBudgetExceeded,
        match="total_reserved_tokens",
    ):
        await controller.run(
            get_mbb_evaluation_corpus(),
            case_ids=("product.discovery.normal",),
            reasoning_profiles=(ProviderReasoningProfile.standard,),
        )

    assert adapter.requests == []


class _SlowProvider(ProviderTurnAdapter):
    provider_name = "slow-test"
    model = "slow-test-model"

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        await asyncio.sleep(0.05)
        return ProviderTurnResult(
            text="late",
            finish_reason=ProviderFinishReason.completed,
        )


@pytest.mark.asyncio
async def test_live_controller_normalizes_transport_timeout_without_payloads():
    budget = LiveEvaluationRunBudget(http_timeout_seconds=0.001)
    controller = _controller(_SlowProvider(), budget=budget)

    with pytest.raises(LiveEvaluationProviderFailure, match="timeout") as captured:
        await controller.run(
            get_mbb_evaluation_corpus(),
            case_ids=("product.discovery.normal",),
            reasoning_profiles=(ProviderReasoningProfile.standard,),
        )

    assert captured.value.evidence.reason == "timeout"
    assert captured.value.evidence.completed_provider_calls == 0


@pytest.mark.asyncio
async def test_live_runner_uses_the_thin_controller_with_a_fake_provider(monkeypatch):
    monkeypatch.setattr(
        run_ai_evaluation,
        "LIVE_JOURNEY_CASE_IDS",
        ("product.discovery.normal",),
    )
    settings = Settings(
        ai_adapter="disabled",
        ai_turn_provider="deepseek",
        deepseek_api_key="test-secret",
        whatsapp_send_enabled=False,
        crm_send_enabled=False,
        payment_send_enabled=False,
        relance_enabled=False,
        scheduled_tasks_enabled=False,
        m1_maps_fanout_enabled=False,
    )
    output = await run_ai_evaluation._run_live(
        _args(),
        configured_settings=settings,
        adapter_factory=lambda: ScriptedProvider(
            _product_search_results()
        ),
        budget=LiveEvaluationRunBudget(max_case_executions=1),
        environment={"DEEPSEEK_API_KEY": "test-secret"},
    )

    payload = json.loads(output)
    assert payload["reports"][0]["aggregate"]["cases_executed"] == 1


def test_live_settings_keep_all_external_effects_closed():
    settings = Settings(
        ai_adapter="disabled",
        ai_turn_provider="deepseek",
        deepseek_api_key="test-secret",
        whatsapp_send_enabled=True,
    )

    with pytest.raises(
        LiveEvaluationConfigurationError,
        match="external_effect_gates_not_disabled",
    ):
        run_ai_evaluation._validate_live_settings(
            settings,
            {"DEEPSEEK_API_KEY": "test-secret"},
        )
