from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    GetProductDetailsOutput,
    RequestHumanHandoffOutput,
    SearchProductsOutput,
)
from app.ai.evaluation import (
    EvaluationCategory,
    EvaluationLanguagePattern,
    EvaluationObservation,
    EvaluationOutcomeClass,
    EvaluationReplay,
    EvaluationRunMetadata,
    RecordedProviderCall,
)
from app.ai.evaluation_corpus import (
    MBB_EVALUATION_CORPUS_VERSION,
    get_mbb_evaluation_corpus,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderReasoningProfile,
    ProviderTurnResult,
)


def test_initial_corpus_is_versioned_representative_and_fictional():
    corpus = get_mbb_evaluation_corpus()

    assert corpus.version == "mbb-ai-eval-v1"
    assert len(corpus.cases) == 24
    assert {category for case in corpus.cases for category in case.categories} == set(
        EvaluationCategory
    )
    assert {case.language_pattern for case in corpus.cases} == set(
        EvaluationLanguagePattern
    )
    assert {
        "product.discovery.normal",
        "product.discovery.vague_need",
        "product.discovery.budget_usd",
        "product.discovery.comparison",
        "product.truth.available",
        "product.truth.out_of_stock",
        "product.truth.nonexistent",
        "product.truth.unsupported_feature",
        "product.truth.current_price",
        "evidence.no_matching_product",
        "evidence.capability_error",
        "evidence.contradictory_price",
        "handoff.explicit_human",
        "handoff.policy_exception",
        "handoff.repeated_unreliable_answer",
        "injection.ignore_rules_discount",
        "injection.pretend_stock",
        "injection.call_hidden_order_tool",
        "unsupported.order_now",
        "unsupported.payment_confirmation",
        "language.informal_french",
        "language.french_lingala",
        "language.french_swahili",
    }.issubset(case.case_id for case in corpus.cases)

    serialized = corpus.model_dump_json().casefold()
    assert "mbb test air fryer" in serialized
    assert "fictional" in serialized
    assert "customer_id" not in serialized
    assert "conversation_id" not in serialized


def test_corpus_exposure_and_fixtures_reuse_registered_capability_contracts():
    corpus = get_mbb_evaluation_corpus()

    for case in corpus.cases:
        assert all(
            AI_CAPABILITY_REGISTRY.resolve(name) is not None
            for name in case.exposed_capabilities
        )
        for fixture in case.capability_fixtures:
            assert fixture.capability_name in case.exposed_capabilities
            if fixture.status == "error":
                assert fixture.output is None
                continue
            payload = json.dumps(fixture.output)
            if fixture.capability_name == "search_products":
                SearchProductsOutput.model_validate_json(payload)
            elif fixture.capability_name == "get_product_details":
                GetProductDetailsOutput.model_validate_json(payload)
            elif fixture.capability_name == "request_human_handoff":
                RequestHumanHandoffOutput.model_validate_json(payload)
            else:  # pragma: no cover - the initial corpus has only approved tools.
                raise AssertionError("unexpected evaluation fixture capability")


def test_corpus_models_expected_tool_arguments_handoff_and_hard_safety():
    cases = {case.case_id: case for case in get_mbb_evaluation_corpus().cases}

    budget = cases["product.discovery.budget_usd"].expectations
    assert budget.capability_arguments[0].arguments == {
        "query": "air fryer",
        "max_budget": 70,
        "budget_currency": "USD",
    }

    handoff = cases["handoff.explicit_human"].expectations
    assert handoff.required_capabilities == ("request_human_handoff",)
    assert handoff.expected_outcomes == (EvaluationOutcomeClass.handoff,)

    order = cases["unsupported.order_now"].expectations
    assert "create_order" in order.forbidden_capabilities
    assert order.forbidden_action_claim_fragments

    payment = cases["unsupported.payment_confirmation"].expectations
    assert "confirm_payment" in payment.forbidden_capabilities
    assert payment.forbidden_action_claim_fragments


def test_offline_cli_scores_replay_and_emits_machine_readable_json(tmp_path):
    replay = EvaluationReplay(
        metadata=EvaluationRunMetadata(
            corpus_version=MBB_EVALUATION_CORPUS_VERSION,
            provider="scripted",
            model="fixture-model",
            model_version="fixture-model-v1",
            reasoning_profile=ProviderReasoningProfile.minimal,
            policy_version=AI_SYSTEM_POLICY_VERSION,
        ),
        observations=(
            EvaluationObservation(
                case_id="product.discovery.vague_need",
                provider_calls=(
                    RecordedProviderCall(
                        result=ProviderTurnResult(
                            text="Quel type d'appareil cherchez-vous pour la cuisine ?",
                            finish_reason=ProviderFinishReason.completed,
                        ),
                        latency_ms=4,
                    ),
                ),
                final_outcome=EvaluationOutcomeClass.clarification,
            ),
        ),
    )
    replay_path = tmp_path / "replay.json"
    replay_path.write_text(replay.model_dump_json(), encoding="utf-8")
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_ai_evaluation.py"
    environment = os.environ.copy()
    environment.update(
        {
            "AI_ADAPTER": "disabled",
            "WHATSAPP_SEND_ENABLED": "false",
            "CRM_SEND_ENABLED": "false",
            "PAYMENT_SEND_ENABLED": "false",
            "RELANCE_ENABLED": "false",
            "SCHEDULED_TASKS_ENABLED": "false",
        }
    )

    completed = subprocess.run(
        [
            sys.executable,
            str(script),
            "--replay",
            str(replay_path),
            "--case-id",
            "product.discovery.vague_need",
            "--pretty",
        ],
        cwd=script.parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    report = json.loads(completed.stdout)
    assert report["metadata"] == {
        "corpus_version": "mbb-ai-eval-v1",
        "provider": "scripted",
        "model": "fixture-model",
        "model_version": "fixture-model-v1",
        "reasoning_profile": "minimal",
        "policy_version": "mbb-ai-policy-v2",
    }
    assert report["aggregate"]["cases_executed"] == 1
    assert report["aggregate"]["deterministic_passes"] == 1
    assert report["aggregate"]["provider_calls"] == 1
    assert report["aggregate"]["capability_calls"] == 0
