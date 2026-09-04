"""Focused offline controls for the AI-5B2 real-journey bridge."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.adapters.base import ProviderTurnAdapter
from app.ai.canary_bridge import (
    AI5B2_CANARIES,
    AI5B2_CANARY_IDS,
    AI5B2BridgeConfigurationError,
    AI5B2BudgetDecision,
    AI5B2BudgetDecisionLimits,
    AI5B2BudgetProfile,
    AI5B2ProviderSelection,
    C01CommercialFacts,
    CanaryRequestReservation,
    CanaryAuthorizationRecord,
    CanaryBridgeEvidence,
    CanaryCaseEvidence,
    CanaryManualReviewStatus,
    CanaryProviderMode,
    CanaryPricingVerificationRecord,
    CanaryReviewerAssignmentRecord,
    CanaryTranscriptEntry,
    CumulativeBudgetProvider,
    conservative_json_request_reservation,
    dry_run_manifest,
    evaluate_c01_commercial_response,
    provider_neutral_request_reservation,
    select_canary_provider,
)
from app.ai.offline_certification import (
    EvaluationDeadlineAdapter,
    ManualEvaluationClock,
    OfflineBudgetExceeded,
    OfflineBudgetLedger,
    redacted_evidence_json,
)
from app.ai.provider_contract import (
    ProviderErrorCategory,
    ProviderContinuationState,
    ProviderFinishReason,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderUsage,
)
import scripts.run_ai5b2_canary_bridge as bridge_script
from scripts.run_ai5b2_canary_bridge import main as bridge_main


_SYNTHETIC_BASELINE = "1" * 40


def _synthetic_budget_decision(
    run_id: str = "synthetic-ai5b2-run",
) -> AI5B2BudgetDecision:
    accepted_at = datetime.now(timezone.utc)
    return AI5B2BudgetDecision(
        decision_id=f"synthetic:budget-decision:{run_id}",
        decision_version="mbb-ai5b2-budget-decision-v1",
        contract_version="mbb-ai5b-contract-v2",
        accepted=True,
        accepted_by="project-owner",
        accepted_at=accepted_at,
        valid_until=accepted_at + timedelta(hours=1),
        run_id=run_id,
        authorization_record_id="synthetic:authorization:ai5b2",
        baseline_commit=_SYNTHETIC_BASELINE,
        limits=AI5B2BudgetDecisionLimits(
            case_executions=4,
            provider_requests=21,
            total_api_tokens=40_000,
            cost_usd=Decimal("0.05"),
            completion_tokens_per_request=512,
            automatic_provider_retries=0,
            evaluation_durable_actions=1,
            ai_turn_provider_calls=3,
            ai_turn_tool_rounds=2,
            ai_turn_capability_executions=3,
            ai_turn_durable_action_attempts=2,
            provider_deadline_seconds=12,
            provider_deadline_scope="provider_request",
            outer_watchdog_seconds=60,
            outer_watchdog_scope="evaluation_operation",
            stage_ceiling_seconds=600,
            stage_ceiling_scope="complete_four_case_stage",
        ),
        admission_method="utf8_wire_bytes_plus_json_nodes_estimate_v1",
        settle_from_complete_api_usage=True,
        retain_unresolved_estimates=True,
        stop_on_missing_or_uncertain_usage=True,
        accepts_single_dispatched_request_token_or_cost_overrun=True,
        accepts_no_subsequent_dispatch_after_overrun=True,
        synthetic=True,
    )


def _synthetic_dispatch_records(run_id: str = "synthetic-ai5b2-run") -> dict:
    return {
        "current_baseline_commit": _SYNTHETIC_BASELINE,
        "authorization": CanaryAuthorizationRecord(
            record_id="synthetic:authorization:ai5b2",
            run_id=run_id,
            baseline_commit=_SYNTHETIC_BASELINE,
            synthetic=True,
        ),
        "pricing_verification": CanaryPricingVerificationRecord(
            record_id="synthetic:pricing:ai5b2",
            source="synthetic://offline-fixture-not-official-pricing",
            verified_at="synthetic-not-a-real-verification-time",
            input_usd_per_million=Decimal("0.50"),
            output_usd_per_million=Decimal("1.00"),
            synthetic=True,
        ),
        "reviewer_assignment": CanaryReviewerAssignmentRecord(
            record_id="synthetic:reviewer-assignment:ai5b2",
            reviewer_id="synthetic:reviewer:not-a-human-review",
            drc_language_familiarity_confirmed=True,
            synthetic=True,
        ),
        "budget_decision": _synthetic_budget_decision(run_id),
        "external_effects_disabled": True,
        "disposable_database_isolated": True,
    }


class _SequenceAdapter(ProviderTurnAdapter):
    provider_name = "fixture"
    model = "offline-fixture"

    def __init__(self, *results: ProviderTurnResult | Exception) -> None:
        self.results = list(results)
        self.calls = 0

    async def generate_turn(self, _request: ProviderTurnRequest) -> ProviderTurnResult:
        result = self.results[self.calls]
        self.calls += 1
        if isinstance(result, Exception):
            raise result
        return result


def _request() -> ProviderTurnRequest:
    return ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="Synthetic bridge request"),),
        system_instruction="Synthetic evaluation-only instruction.",
        max_output_tokens=512,
    )


def _result(*, usage: ProviderUsage | None = None) -> ProviderTurnResult:
    return ProviderTurnResult(
        text="Synthetic result",
        finish_reason=ProviderFinishReason.completed,
        usage=usage,
    )


def _c01_facts(*, freshness_verified: bool = True) -> C01CommercialFacts:
    return C01CommercialFacts(
        product_name="MBB Test Air Fryer",
        sellable_model_label="6L",
        usd_price=Decimal("55.00"),
        cdf_price=Decimal("154000.00"),
        availability="available",
        is_sellable_now=True,
        freshness_verified=freshness_verified,
    )


def test_frozen_cases_and_default_cli_are_zero_dispatch(monkeypatch, capsys) -> None:
    assert AI5B2_CANARY_IDS == (
        "B2-C01-FR-FRESH-P6",
        "B2-C02-FR-QUALIFIED",
        "B2-C03-FR-INJECTION-P8",
        "B2-C04-SW-FR-BUDGET",
    )
    assert AI5B2_CANARIES[-1].customer_message == (
        "Finalement bajeti ni 45 dollars; una option moins chère?"
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ambient-must-not-activate")

    assert bridge_main(()) == 0
    output = capsys.readouterr().out
    assert output.strip() == dry_run_manifest()
    assert '"mode":"dry_run"' in output
    assert '"provider_dispatches":0' in output
    assert "ambient-must-not-activate" not in output


def test_live_validation_precedes_credentials_and_dispatch() -> None:
    credential_loads = 0
    constructions = 0

    def load_credential() -> str:
        nonlocal credential_loads
        credential_loads += 1
        return "inert-placeholder"

    def construct(_credential: str) -> ProviderTurnAdapter:
        nonlocal constructions
        constructions += 1
        return _SequenceAdapter(_result())

    with pytest.raises(AI5B2BridgeConfigurationError) as missing_opt_in:
        select_canary_provider(
            AI5B2ProviderSelection(mode=CanaryProviderMode.live),
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=construct,
        )
    assert missing_opt_in.value.safe_code == "explicit_live_opt_in_required"
    assert credential_loads == 0 and constructions == 0

    insufficient = AI5B2ProviderSelection(
        mode=CanaryProviderMode.live,
        explicit_live_opt_in=True,
        run_id="ai5b2-valid-run",
        current_baseline_commit=_SYNTHETIC_BASELINE,
        external_effects_disabled=True,
        disposable_database_isolated=True,
        budget=AI5B2BudgetProfile(max_provider_calls=6),
    )
    with pytest.raises(AI5B2BridgeConfigurationError) as budget_failure:
        select_canary_provider(
            insufficient,
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=construct,
        )
    assert budget_failure.value.safe_code == "provider_call_budget_insufficient"
    assert credential_loads == 0 and constructions == 0

    selected = select_canary_provider(
        AI5B2ProviderSelection(
            mode=CanaryProviderMode.offline_mocked_http,
            explicit_live_opt_in=True,
            run_id="synthetic-ai5b2-run",
            **_synthetic_dispatch_records(),
        ),
        offline_factory=lambda: _SequenceAdapter(_result()),
        credential_loader=load_credential,
        live_factory=construct,
    )
    assert isinstance(selected, _SequenceAdapter)
    assert credential_loads == 1 and constructions == 1
    assert selected.calls == 0

    with pytest.raises(AI5B2BridgeConfigurationError) as missing_pricing:
        select_canary_provider(
            AI5B2ProviderSelection(
                mode=CanaryProviderMode.offline_mocked_http,
                explicit_live_opt_in=True,
                run_id="synthetic-ai5b2-run",
                **{
                    **_synthetic_dispatch_records(),
                    "pricing_verification": None,
                },
            ),
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=construct,
        )
    assert missing_pricing.value.safe_code == "official_pricing_not_verified"
    assert credential_loads == 1 and constructions == 1

    with pytest.raises(AI5B2BridgeConfigurationError) as synthetic_live:
        select_canary_provider(
            AI5B2ProviderSelection(
                mode=CanaryProviderMode.live,
                explicit_live_opt_in=True,
                run_id="synthetic-ai5b2-run",
                **_synthetic_dispatch_records(),
            ),
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=construct,
        )
    assert synthetic_live.value.safe_code == "synthetic_record_forbidden_in_live_mode"
    assert credential_loads == 1 and constructions == 1

    live_records = _synthetic_dispatch_records()
    live_records["authorization"] = live_records["authorization"].model_copy(
        update={"synthetic": False}
    )
    live_records["pricing_verification"] = live_records[
        "pricing_verification"
    ].model_copy(update={"synthetic": False})
    live_records["reviewer_assignment"] = live_records[
        "reviewer_assignment"
    ].model_copy(update={"synthetic": False})
    live_records["budget_decision"] = live_records["budget_decision"].model_copy(
        update={"synthetic": False}
    )
    missing_decision_records = {**live_records, "budget_decision": None}
    with pytest.raises(AI5B2BridgeConfigurationError) as missing_decision:
        select_canary_provider(
            AI5B2ProviderSelection(
                mode=CanaryProviderMode.live,
                explicit_live_opt_in=True,
                run_id="synthetic-ai5b2-run",
                **missing_decision_records,
            ),
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=construct,
        )
    assert missing_decision.value.safe_code == "budget_decision_required"
    assert credential_loads == 1 and constructions == 1

    false_decision_records = {
        **live_records,
        "budget_decision": live_records["budget_decision"].model_copy(
            update={"accepted": False}
        ),
    }
    with pytest.raises(AI5B2BridgeConfigurationError) as false_decision:
        select_canary_provider(
            AI5B2ProviderSelection(
                mode=CanaryProviderMode.live,
                explicit_live_opt_in=True,
                run_id="synthetic-ai5b2-run",
                **false_decision_records,
            ),
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=construct,
        )
    assert false_decision.value.safe_code == "budget_decision_not_accepted"
    assert credential_loads == 1 and constructions == 1

    selected_live = select_canary_provider(
        AI5B2ProviderSelection(
            mode=CanaryProviderMode.live,
            explicit_live_opt_in=True,
            run_id="synthetic-ai5b2-run",
            **live_records,
        ),
        offline_factory=lambda: _SequenceAdapter(_result()),
        credential_loader=load_credential,
        live_factory=construct,
    )
    assert isinstance(selected_live, _SequenceAdapter)
    assert credential_loads == 2 and constructions == 2


def test_budget_decision_is_exact_fresh_and_not_reusable_for_another_run() -> None:
    credential_loads = 0

    def load_credential() -> str:
        nonlocal credential_loads
        credential_loads += 1
        return "inert-placeholder"

    base_records = _synthetic_dispatch_records()
    base_selection = AI5B2ProviderSelection(
        mode=CanaryProviderMode.offline_mocked_http,
        explicit_live_opt_in=True,
        run_id="synthetic-ai5b2-run",
        **base_records,
    )

    mismatches = (
        (
            "budget_decision_commit_mismatch",
            {"baseline_commit": "2" * 40},
        ),
        ("budget_decision_run_mismatch", {"run_id": "synthetic-other-run"}),
        (
            "budget_decision_authorization_mismatch",
            {"authorization_record_id": "synthetic:authorization:other"},
        ),
        (
            "budget_decision_contract_mismatch",
            {"contract_version": "mbb-ai5b-contract-v1"},
        ),
        (
            "budget_decision_version_mismatch",
            {"decision_version": "mbb-ai5b2-budget-decision-v0"},
        ),
        (
            "budget_decision_limits_mismatch",
            {
                "limits": base_records["budget_decision"].limits.model_copy(
                    update={"provider_requests": 20}
                )
            },
        ),
        (
            "budget_decision_stale",
            {
                "accepted_at": datetime.now(timezone.utc) - timedelta(hours=2),
                "valid_until": datetime.now(timezone.utc) - timedelta(hours=1),
            },
        ),
    )
    for expected_code, update in mismatches:
        selection = base_selection.model_copy(
            update={
                "budget_decision": base_records["budget_decision"].model_copy(
                    update=update
                )
            }
        )
        with pytest.raises(AI5B2BridgeConfigurationError) as failure:
            select_canary_provider(
                selection,
                offline_factory=lambda: _SequenceAdapter(_result()),
                credential_loader=load_credential,
                live_factory=lambda _credential: _SequenceAdapter(_result()),
            )
        assert failure.value.safe_code == expected_code
    assert credential_loads == 0

    reused_records = _synthetic_dispatch_records("synthetic-other-run")
    reused_records["budget_decision"] = base_records["budget_decision"]
    with pytest.raises(AI5B2BridgeConfigurationError) as reused:
        select_canary_provider(
            AI5B2ProviderSelection(
                mode=CanaryProviderMode.offline_mocked_http,
                explicit_live_opt_in=True,
                run_id="synthetic-other-run",
                **reused_records,
            ),
            offline_factory=lambda: _SequenceAdapter(_result()),
            credential_loader=load_credential,
            live_factory=lambda _credential: _SequenceAdapter(_result()),
        )
    assert reused.value.safe_code == "budget_decision_run_mismatch"
    assert credential_loads == 0


def test_budget_decision_acceptance_has_no_default() -> None:
    decision = _synthetic_budget_decision().model_dump()
    decision.pop("accepted")
    with pytest.raises(ValidationError, match="accepted"):
        AI5B2BudgetDecision.model_validate(decision)


def test_cli_rejects_missing_false_and_malformed_decisions_before_database_or_credential(
    tmp_path, monkeypatch, capsys
) -> None:
    database_prepares = 0
    credential_loads = 0

    def prepare(_runtime) -> None:
        nonlocal database_prepares
        database_prepares += 1

    def load_credential() -> str:
        nonlocal credential_loads
        credential_loads += 1
        return "inert-placeholder"

    monkeypatch.setattr(bridge_script.DisposablePostgresRuntime, "prepare", prepare)
    monkeypatch.setattr(
        bridge_script.DisposablePostgresRuntime,
        "cleanup",
        lambda _runtime: None,
    )
    monkeypatch.setattr(bridge_script, "_credential_loader", load_credential)

    common = (
        "--live",
        "--authorize-live",
        "--authorized-baseline",
        _SYNTHETIC_BASELINE,
        "--authorization-record-id",
        "synthetic:authorization:ai5b2",
        "--external-effects-disabled",
        "--evidence-root",
        str(tmp_path),
    )
    cases = []
    missing_run = "synthetic-missing-decision"
    cases.append((missing_run, (), "budget_decision_required"))

    false_run = "synthetic-false-decision"
    false_path = tmp_path / "false-decision.json"
    false_path.write_text(
        _synthetic_budget_decision(false_run)
        .model_copy(
            update={
                "accepted": False,
                "baseline_commit": _SYNTHETIC_BASELINE,
            }
        )
        .model_dump_json(),
        encoding="utf-8",
    )
    cases.append(
        (
            false_run,
            ("--budget-decision-file", str(false_path)),
            "budget_decision_not_accepted",
        )
    )

    malformed_run = "synthetic-malformed-decision"
    malformed_path = tmp_path / "malformed-decision.json"
    malformed_path.write_text(
        '{"accepted":true,"secret":"must-not-persist"}', encoding="utf-8"
    )
    cases.append(
        (
            malformed_run,
            ("--budget-decision-file", str(malformed_path)),
            "budget_decision_malformed",
        )
    )

    for run_id, decision_args, expected_code in cases:
        assert bridge_main((*common, "--run-id", run_id, *decision_args)) == 1
        evidence = (tmp_path / run_id / "evidence.json").read_text(encoding="utf-8")
        assert expected_code in evidence
        assert "must-not-persist" not in evidence

    capsys.readouterr()
    assert database_prepares == 0
    assert credential_loads == 0


@pytest.mark.asyncio
async def test_run_budget_is_cumulative_and_missing_usage_fails_safely() -> None:
    usage = ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25)
    profile = AI5B2BudgetProfile(max_provider_calls=1)
    ledger = OfflineBudgetLedger(profile.offline_limits())
    inner = _SequenceAdapter(_result(usage=usage), _result(usage=usage))
    adapter = CumulativeBudgetProvider(inner, ledger=ledger, profile=profile)

    await adapter.generate_turn(_request())
    with pytest.raises(OfflineBudgetExceeded, match="provider_calls"):
        await adapter.generate_turn(_request())
    with pytest.raises(AI5B2BridgeConfigurationError, match="stage_dispatch_stopped"):
        await adapter.generate_turn(_request())

    assert inner.calls == 1
    assert adapter.dispatched_requests == 1
    assert ledger.provider_calls == 1
    assert ledger.observed_tokens == 25
    reservation = provider_neutral_request_reservation(_request())
    assert ledger.reserved_tokens == reservation.total_tokens
    assert ledger.unresolved_reserved_tokens == 0
    assert ledger.committed_tokens == 25

    missing_profile = AI5B2BudgetProfile()
    missing_ledger = OfflineBudgetLedger(missing_profile.offline_limits())
    missing_inner = _SequenceAdapter(_result())
    missing = CumulativeBudgetProvider(
        missing_inner,
        ledger=missing_ledger,
        profile=missing_profile,
    )
    with pytest.raises(ProviderTurnError) as missing_failure:
        await missing.generate_turn(_request())
    assert missing_failure.value.category == ProviderErrorCategory.malformed_response
    assert missing.dispatched_requests == 1
    assert missing.missing_usage_failures == 1
    assert missing_ledger.provider_calls == 1
    assert missing_ledger.observed_tokens == 0
    assert missing_ledger.reserved_tokens == reservation.total_tokens
    assert missing_ledger.unresolved_reserved_tokens == reservation.total_tokens
    assert missing_ledger.committed_tokens == reservation.total_tokens
    assert missing.call_evidence[0].outcome == "failed"
    assert missing.call_evidence[0].total_tokens is None
    assert missing.call_evidence[0].estimated_cost_usd is None
    with pytest.raises(AI5B2BridgeConfigurationError, match="stage_dispatch_stopped"):
        await missing.generate_turn(_request())
    assert missing_inner.calls == 1


@pytest.mark.asyncio
async def test_bridge_deadline_observes_and_discards_real_late_completion() -> None:
    clock = ManualEvaluationClock()
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    produced = asyncio.Event()
    usage = ProviderUsage(input_tokens=10, output_tokens=3, total_tokens=13)

    class CancellationResistantAdapter(ProviderTurnAdapter):
        provider_name = "fixture"
        model = "offline-late"

        async def generate_turn(
            self, _request: ProviderTurnRequest
        ) -> ProviderTurnResult:
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            produced.set()
            return _result(usage=usage)

    profile = AI5B2BudgetProfile()
    ledger = OfflineBudgetLedger(profile.offline_limits())
    budgeted = CumulativeBudgetProvider(
        CancellationResistantAdapter(),
        ledger=ledger,
        profile=profile,
    )
    controller = EvaluationDeadlineAdapter(
        budgeted,
        clock=clock,
        on_timeout=budgeted.mark_current_request_timed_out,
    )
    pending = asyncio.create_task(controller.generate_turn(_request()))
    await asyncio.wait_for(started.wait(), timeout=1)

    async def wait_for_deadline_timer() -> None:
        while clock.pending_timer_count == 0:
            await asyncio.sleep(0)

    await asyncio.wait_for(wait_for_deadline_timer(), timeout=1)

    await clock.advance(11.999)
    assert not pending.done() and not produced.is_set()
    await clock.advance(0.001)
    with pytest.raises(ProviderTurnError) as timeout:
        await asyncio.wait_for(pending, timeout=1)
    assert timeout.value.category == ProviderErrorCategory.timeout
    await asyncio.wait_for(cancelled.wait(), timeout=1)
    assert ledger.provider_calls == 1 and budgeted.dispatched_requests == 1

    release.set()
    await asyncio.wait_for(produced.wait(), timeout=1)
    await asyncio.wait_for(controller.drain_late_completions(), timeout=1)
    assert controller.late_completions_observed == 1
    assert controller.late_completions_discarded == 1
    assert ledger.observed_tokens == 13
    assert budgeted.stop_latch.stop_reason == "provider_timeout"
    assert budgeted.call_evidence[0].outcome == "timed_out"
    assert budgeted.call_evidence[0].total_tokens == 13
    assert controller.unfinished_task_count == 0
    assert clock.pending_timer_count == 0


@pytest.mark.asyncio
async def test_malformed_provider_failure_keeps_precall_reservation() -> None:
    profile = AI5B2BudgetProfile()
    ledger = OfflineBudgetLedger(profile.offline_limits())
    inner = _SequenceAdapter(
        ProviderTurnError(ProviderErrorCategory.malformed_response)
    )
    adapter = CumulativeBudgetProvider(inner, ledger=ledger, profile=profile)

    with pytest.raises(ProviderTurnError) as malformed:
        await adapter.generate_turn(_request())
    assert malformed.value.category == ProviderErrorCategory.malformed_response
    assert adapter.dispatched_requests == 1
    assert ledger.provider_calls == 1
    reservation = provider_neutral_request_reservation(_request())
    assert ledger.reserved_tokens == reservation.total_tokens
    assert ledger.unresolved_reserved_tokens == reservation.total_tokens
    assert ledger.committed_tokens == reservation.total_tokens
    assert adapter.call_evidence[0].outcome == "failed"
    with pytest.raises(AI5B2BridgeConfigurationError, match="stage_dispatch_stopped"):
        await adapter.generate_turn(_request())
    assert inner.calls == 1


@pytest.mark.parametrize(
    "response",
    (
        "Le MBB Test Air Fryer 6L est dispo à 55 USD.",
        "Le MBB Test Air Fryer 6L est disponible à 55 dollars.",
        (
            "Oui, le MBB Test Air Fryer 6L est dispo ✅ à 55 $ "
            "(154 000 FC). Tu veux plus d'infos dessus ?"
        ),
    ),
)
def test_c01_truth_evaluator_accepts_supported_paraphrases(response) -> None:
    result = evaluate_c01_commercial_response(response, facts=_c01_facts())
    assert result.status == "passed"
    assert result.false_claims == ()
    assert result.review_reasons == ()


@pytest.mark.parametrize(
    ("response", "reason"),
    (
        (
            "Le MBB Test Air Fryer 6L n'est pas dispo à 55 USD.",
            "false_unavailability",
        ),
        (
            "Le MBB Test Air Fryer 6L est indisponible à 55 USD.",
            "false_unavailability",
        ),
        (
            "Le MBB Test Air Fryer 6L est disponible mais pas dispo à 55 USD.",
            "contradictory_availability",
        ),
        (
            "Le MBB Test Air Fryer 8L est disponible à 55 USD.",
            "wrong_product_model",
        ),
        (
            "Le MBB Test Air Fryer 6L est disponible à 54 USD.",
            "wrong_usd_price",
        ),
        (
            "Le MBB Test Air Fryer 6L est disponible à 55 CDF.",
            "wrong_cdf_price",
        ),
        (
            "Le MBB Test Air Fryer 6L est disponible à 55 USD et 50 EUR.",
            "unsupported_currency_claim",
        ),
        (
            "Le MBB Test Air Fryer 6L est disponible à 55 USD avec 10 % de remise.",
            "unsupported_discount_or_concession",
        ),
        (
            "Le MBB Test Air Fryer 6L est disponible à 55 USD; je vous rappelle demain.",
            "unsupported_follow_up_commitment",
        ),
    ),
)
def test_c01_truth_evaluator_rejects_proven_false_claims(response, reason) -> None:
    result = evaluate_c01_commercial_response(response, facts=_c01_facts())
    assert result.status == "failed"
    assert reason in result.false_claims


def test_c01_truth_evaluator_requires_review_for_unrecognized_formulation() -> None:
    result = evaluate_c01_commercial_response(
        "Le MBB Test Air Fryer 6L peut partir pour cinquante-cinq dollars.",
        facts=_c01_facts(),
    )
    assert result.status == "needs_review"
    assert "availability_formulation_unrecognized" in result.review_reasons


def test_c01_truth_evaluator_rejects_missing_freshness() -> None:
    result = evaluate_c01_commercial_response(
        "Le MBB Test Air Fryer 6L est dispo à 55 USD.",
        facts=_c01_facts(freshness_verified=False),
    )
    assert result.status == "failed"
    assert "fresh_product_offer_missing" in result.hard_gate_failures


def test_request_reservation_grows_with_tools_results_and_continuation() -> None:
    first = conservative_json_request_reservation(
        {"messages": [{"role": "user", "content": "x"}]},
        max_output_tokens=512,
    )
    growing_payload = {
        "messages": [
            {"role": "user", "content": "x" * 2_000},
            {"role": "assistant", "reasoning_content": "hidden" * 500},
            {"role": "tool", "content": "result" * 500},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "search_products",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
        ],
    }
    second = conservative_json_request_reservation(
        growing_payload,
        max_output_tokens=512,
    )
    assert second.input_tokens > first.input_tokens
    assert second.total_tokens == second.input_tokens + 512
    assert second.version == "mbb-ai5b2-request-estimate-v3"
    assert second.method == "utf8_wire_bytes_plus_json_nodes_estimate_v1"
    assert second.basis == "admission_estimate_not_verified_maximum"
    assert "hidden" not in second.model_dump_json()

    continuation_request = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="Synthetic"),),
        system_instruction="System",
        max_output_tokens=512,
        continuation_state=ProviderContinuationState(
            value={"opaque": "sensitive-continuation" * 100}
        ),
    )
    without_continuation = continuation_request.model_copy(
        update={"continuation_state": None}
    )
    assert (
        provider_neutral_request_reservation(continuation_request).input_tokens
        > provider_neutral_request_reservation(without_continuation).input_tokens
    )


@pytest.mark.asyncio
async def test_request_over_remaining_capacity_is_blocked_before_transport() -> None:
    proposal = provider_neutral_request_reservation(_request())
    profile = AI5B2BudgetProfile(max_total_tokens=proposal.total_tokens - 1)
    ledger = OfflineBudgetLedger(profile.offline_limits())
    inner = _SequenceAdapter(
        _result(usage=ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25))
    )
    adapter = CumulativeBudgetProvider(inner, ledger=ledger, profile=profile)

    with pytest.raises(OfflineBudgetExceeded, match="total_tokens"):
        await adapter.generate_turn(_request())
    assert inner.calls == 0
    assert adapter.dispatched_requests == 0
    assert ledger.provider_calls == 0
    assert adapter.call_evidence[0].transport_dispatched is False
    assert adapter.call_evidence[0].reserved_tokens == proposal.total_tokens
    assert adapter.stop_latch.stop_reason == "budget_total_tokens"


@pytest.mark.asyncio
async def test_known_usage_settles_without_double_counting_next_reservation() -> None:
    proposal = provider_neutral_request_reservation(_request())
    usage = ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=25)
    profile = AI5B2BudgetProfile(
        max_provider_calls=2,
        max_total_tokens=proposal.total_tokens + usage.total_tokens,
    )
    ledger = OfflineBudgetLedger(profile.offline_limits())
    inner = _SequenceAdapter(_result(usage=usage), _result(usage=usage))
    adapter = CumulativeBudgetProvider(inner, ledger=ledger, profile=profile)

    await adapter.generate_turn(_request())
    await adapter.generate_turn(_request())

    assert inner.calls == 2
    assert ledger.reserved_tokens == proposal.total_tokens * 2
    assert ledger.observed_tokens == 50
    assert ledger.unresolved_reserved_tokens == 0
    assert ledger.committed_tokens == 50


@pytest.mark.asyncio
async def test_under_reservation_settles_usage_and_latches_stage() -> None:
    usage = ProviderUsage(input_tokens=100, output_tokens=500, total_tokens=600)
    profile = AI5B2BudgetProfile()
    ledger = OfflineBudgetLedger(profile.offline_limits())
    inner = _SequenceAdapter(_result(usage=usage), _result(usage=usage))

    def insufficient(_request: ProviderTurnRequest) -> CanaryRequestReservation:
        return CanaryRequestReservation(
            input_tokens=1,
            output_tokens=512,
            serialized_utf8_bytes=1,
            structural_nodes=1,
        )

    adapter = CumulativeBudgetProvider(
        inner,
        ledger=ledger,
        profile=profile,
        request_reservation=insufficient,
    )
    with pytest.raises(OfflineBudgetExceeded, match="reservation_violation"):
        await adapter.generate_turn(_request())
    assert ledger.observed_tokens == 600
    assert ledger.unresolved_reserved_tokens == 0
    assert ledger.reservation_violations == 1
    assert adapter.call_evidence[0].reservation_settled is True
    assert adapter.call_evidence[0].reservation_violation is True
    assert adapter.stop_latch.stop_reason == "budget_reservation_violation"
    with pytest.raises(AI5B2BridgeConfigurationError, match="stage_dispatch_stopped"):
        await adapter.generate_turn(_request())
    assert inner.calls == 1


@pytest.mark.asyncio
async def test_inconsistent_usage_retains_reservation_and_stops() -> None:
    usage = ProviderUsage(input_tokens=20, output_tokens=5, total_tokens=30)
    profile = AI5B2BudgetProfile()
    ledger = OfflineBudgetLedger(profile.offline_limits())
    inner = _SequenceAdapter(_result(usage=usage))
    adapter = CumulativeBudgetProvider(inner, ledger=ledger, profile=profile)
    proposal = provider_neutral_request_reservation(_request())

    with pytest.raises(ProviderTurnError) as failure:
        await adapter.generate_turn(_request())
    assert failure.value.category == ProviderErrorCategory.malformed_response
    assert ledger.observed_tokens == 0
    assert ledger.unresolved_reserved_tokens == proposal.total_tokens
    assert adapter.stop_latch.stop_reason == "provider_usage_inconsistent"
    assert adapter.call_evidence[0].failure_code == "provider_usage_inconsistent"


def test_evidence_redaction_and_live_manual_review_gate() -> None:
    case = CanaryCaseEvidence(
        case_id="B2-C04-SW-FR-BUDGET",
        fixture_snapshot={"budget_usd": "45"},
        transcript=(
            CanaryTranscriptEntry(
                direction="inbound",
                content="Finalement bajeti ni 45 dollars; una option moins chère?",
            ),
        ),
        validated_tools=("search_products",),
        freshness_verified=True,
        persistence={"outbound_messages": 1},
        finish_reasons=(ProviderFinishReason.completed,),
        deterministic_status="passed",
        manual_review_status=CanaryManualReviewStatus.pending,
        requires_drc_fluent_review=True,
    )
    common = {
        "provider": "deepseek",
        "model": "deepseek-v4-flash",
        "reasoning_profile": ProviderReasoningProfile.default,
        "configured_provider_deadline_seconds": 12,
        "configured_outer_watchdog_seconds": 60,
        "configured_stage_ceiling_seconds": 600,
        "reserved_provider_calls": 1,
        "reserved_tokens": 2_560,
        "reserved_cost_usd": Decimal("0.001536"),
        "observed_total_tokens": 25,
        "cases": (case,),
    }
    offline = CanaryBridgeEvidence(
        evidence_label=CanaryProviderMode.offline_mocked_http,
        overall_decision="offline_bridge_validated",
        **common,
    )
    serialized = redacted_evidence_json(
        {
            "report": offline,
            "reasoning_content": "hidden-must-not-survive",
            "secret": "secret-must-not-survive",
        }
    )
    assert "hidden-must-not-survive" not in serialized
    assert "secret-must-not-survive" not in serialized
    assert "45 dollars" in serialized

    with pytest.raises(ValidationError, match="Human review"):
        CanaryBridgeEvidence(
            evidence_label=CanaryProviderMode.live,
            overall_decision="pass",
            **common,
        )
