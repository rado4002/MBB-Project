"""Focused offline controls for the AI-5B2 real-journey bridge."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.adapters.base import ProviderTurnAdapter
from app.ai.canary_bridge import (
    AI5B2_CANARIES,
    AI5B2_CANARY_IDS,
    AI5B2BridgeConfigurationError,
    AI5B2BudgetProfile,
    AI5B2ProviderSelection,
    CanaryAuthorizationRecord,
    CanaryBridgeEvidence,
    CanaryCaseEvidence,
    CanaryManualReviewStatus,
    CanaryProviderMode,
    CanaryPricingVerificationRecord,
    CanaryReviewerAssignmentRecord,
    CanaryTranscriptEntry,
    CumulativeBudgetProvider,
    dry_run_manifest,
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
    ProviderFinishReason,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderUsage,
)
from scripts.run_ai5b2_canary_bridge import main as bridge_main


_SYNTHETIC_BASELINE = "1" * 40


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
    assert ledger.reserved_tokens == profile.reserved_tokens_per_call
    assert ledger.reserved_cost_usd == profile.fixture_reserved_cost_per_call

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
    assert missing_ledger.reserved_tokens == missing_profile.reserved_tokens_per_call
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
    assert ledger.reserved_tokens == profile.reserved_tokens_per_call
    assert ledger.reserved_cost_usd == profile.fixture_reserved_cost_per_call
    assert adapter.call_evidence[0].outcome == "failed"
    with pytest.raises(AI5B2BridgeConfigurationError, match="stage_dispatch_stopped"):
        await adapter.generate_turn(_request())
    assert inner.calls == 1


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
