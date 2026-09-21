from __future__ import annotations

import uuid

import pytest

from app.ai.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    SafeCapabilityError,
    StrictCapabilityModel,
)
from app.ai.evaluation import EvaluationOutcomeClass
from app.ai.journey_harness import (
    ProductionJourneyHarness,
    ProductionJourneyObservationSource,
    ScriptedProvider,
    fixture_registry,
)
from app.ai.evaluation_corpus import get_mbb_evaluation_corpus
from app.ai.production_journey_corpus import (
    PRODUCTION_JOURNEY_CONTRACT,
    JourneyScenario,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService


class _EchoInput(StrictCapabilityModel):
    value: str


class _EchoOutput(StrictCapabilityModel):
    value: str


def _turn(**overrides) -> AITurn:
    values = {
        "user_content": "Bonjour",
        "language": "french",
        "expected_ownership_version": 1,
        "conversation_id": uuid.UUID("10000000-0000-4000-8000-000000000001"),
    }
    values.update(overrides)
    return AITurn(**values)


def _text(text: str = "Réponse") -> ProviderTurnResult:
    return ProviderTurnResult(text=text, finish_reason=ProviderFinishReason.completed)


@pytest.mark.asyncio
async def test_harness_uses_production_service_for_direct_response():
    provider = ScriptedProvider([_text()])
    harness = ProductionJourneyHarness(provider)

    execution = await harness.execute(
        type("Case", (), {"case_id": "direct"})(),
        _turn(),
    )

    assert execution.finalized is not None
    assert execution.finalized.text == "Réponse"
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_stale_authority_is_rejected_before_provider_call():
    provider = ScriptedProvider([_text()])

    async def stale(_context):
        return False

    service = AITurnService(
        provider,
        authority_checker=stale,
        capability_registry=CapabilityRegistry(()),
    )
    harness = ProductionJourneyHarness(provider, service=service)
    execution = await harness.execute(
        type("Case", (), {"case_id": "stale"})(),
        _turn(allowed_capabilities=()),
    )

    assert execution.execution_error is not None
    assert execution.provider_calls == ()
    assert provider.requests == []


@pytest.mark.asyncio
async def test_nonterminal_capability_continuation_stays_in_production_loop():
    async def echo(_context, arguments):
        return {"value": arguments.value}

    registry = CapabilityRegistry(
        (
            CapabilityDefinition(
                name="echo_value",
                description="Return a deterministic value.",
                input_model=_EchoInput,
                output_model=_EchoOutput,
                handler=echo,
            ),
        )
    )
    provider = ScriptedProvider(
        [
            ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="call_1",
                        capability_name="echo_value",
                        arguments={"value": "ok"},
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
            ),
            _text("Après l'outil"),
        ]
    )
    service = AITurnService(
        provider,
        capability_registry=registry,
        authority_checker=lambda _context: _allowed(),
    )
    harness = ProductionJourneyHarness(provider, service=service)
    execution = await harness.execute(
        type("Case", (), {"case_id": "continuation"})(),
        _turn(allowed_capabilities=("echo_value",)),
    )

    assert execution.finalized is not None
    assert execution.finalized.text == "Après l'outil"
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_fixture_failure_is_observed_through_the_production_executor():
    provider = ScriptedProvider(
        [
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
            _text("Je ne peux pas vérifier le catalogue maintenant."),
        ]
    )
    service = AITurnService(
        provider,
        capability_registry=fixture_registry(
            {"search_products": SafeCapabilityError("catalogue_unavailable")}
        ),
        authority_checker=lambda _context: _allowed(),
    )
    harness = ProductionJourneyHarness(provider, service=service)

    execution = await harness.execute(
        type("Case", (), {"case_id": "capability_failure"})(),
        _turn(allowed_capabilities=("search_products",)),
    )

    assert execution.finalized is not None
    assert execution.finalized.audit_record.capability_activity[0].safe_code == (
        "catalogue_unavailable"
    )
    assert provider.requests[1].messages[-1].role == "tool_result"


@pytest.mark.asyncio
async def test_observation_source_feeds_scoring_without_executing_business_logic():
    case = next(
        item
        for item in get_mbb_evaluation_corpus().cases
        if item.case_id == "product.discovery.vague_need"
    )
    provider = ScriptedProvider([_text("Je peux vous aider.")])
    source = ProductionJourneyObservationSource(
        provider,
        service_factory=lambda adapter: AITurnService(
            adapter,
            authority_checker=lambda _context: _allowed(),
        ),
    )

    observation = await source.observe(case)

    assert observation.case_id == case.case_id
    assert observation.final_outcome == EvaluationOutcomeClass.answer
    assert len(provider.requests) == 1


async def _allowed() -> bool:
    return True


def test_current_contract_covers_production_journey_surface_and_languages():
    assert PRODUCTION_JOURNEY_CONTRACT.version == "mbb-production-journey-v1"
    assert set(PRODUCTION_JOURNEY_CONTRACT.evaluated_languages) == {
        "french",
        "lingala",
        "swahili",
    }
    assert {
        JourneyScenario.product_search,
        JourneyScenario.product_details_continuation,
        JourneyScenario.order_draft_preparation,
        JourneyScenario.exact_confirm_cancel,
        JourneyScenario.stale_authority_rejection,
        JourneyScenario.timeout_recovery,
    }.issubset(PRODUCTION_JOURNEY_CONTRACT.scenarios)
