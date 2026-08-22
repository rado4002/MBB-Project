"""Provider-neutral, hard-budgeted observation source for isolated live evaluation."""
from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

from pydantic import Field

from app.adapters.base import ProviderTurnAdapter
from app.ai.capabilities import AI_CAPABILITY_REGISTRY, CapabilityRegistry
from app.ai.evaluation import (
    EvaluationCapabilityFixture,
    EvaluationCase,
    EvaluationIdentifier,
    EvaluationLanguagePattern,
    EvaluationObservation,
    EvaluationOutcomeClass,
    EvaluationReport,
    RecordedProviderCall,
    StrictEvaluationModel,
)
from app.ai.policy import get_system_policy
from app.ai.provider_contract import (
    ProviderCapability,
    ProviderFinishReason,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderToolError,
    ProviderToolResult,
    ProviderTurnError,
    ProviderTurnRequest,
)

FIRST_LIVE_CANARY_CASE_IDS = (
    "product.discovery.budget_usd",
    "product.discovery.normal",
    "handoff.explicit_human",
    "injection.pretend_stock",
    "language.french_lingala",
)
FIRST_LIVE_CANARY_PROFILES = (
    ProviderReasoningProfile.minimal,
    ProviderReasoningProfile.standard,
    ProviderReasoningProfile.strong,
    ProviderReasoningProfile.default,
)

_POLICY_LANGUAGE_BY_PATTERN = {
    EvaluationLanguagePattern.english: "french",
    EvaluationLanguagePattern.french: "french",
    EvaluationLanguagePattern.informal_french: "french",
    EvaluationLanguagePattern.french_lingala: "lingala",
    EvaluationLanguagePattern.french_swahili: "swahili",
}


class LiveEvaluationRunBudget(StrictEvaluationModel):
    """First-canary limits that can only be tightened by a caller."""

    max_case_executions: int = Field(default=20, ge=1, le=20)
    max_total_provider_calls: int = Field(default=40, ge=1, le=40)
    max_provider_calls_per_case: int = Field(default=2, ge=1, le=2)
    max_tool_rounds_per_case: int = Field(default=1, ge=1, le=1)
    max_capability_executions_per_case: int = Field(default=1, ge=1, le=1)
    max_output_tokens_per_call: int = Field(default=512, ge=1, le=512)
    wall_clock_seconds: int = Field(default=2700, ge=1, le=2700)
    http_timeout_seconds: int = Field(default=60, ge=1, le=60)
    transport_retries: Literal[0] = 0


class LiveEvaluationFailureEvidence(StrictEvaluationModel):
    """Bounded partial evidence retained when any live budget is exceeded."""

    case_id: EvaluationIdentifier
    exceeded_budget: EvaluationIdentifier
    configured_limit: int | float = Field(gt=0)
    observed_value: int | float = Field(ge=0)
    completed_provider_calls: int = Field(ge=0)
    completed_tool_rounds: int = Field(ge=0)
    completed_capability_executions: int = Field(ge=0)
    provider_call_evidence: tuple[RecordedProviderCall, ...] = ()
    tool_result_evidence: tuple[ProviderToolResult, ...] = ()
    first_tool_call: ProviderToolCall | None = None
    first_tool_result: ProviderToolResult | None = None
    rejected_tool_calls: tuple[ProviderToolCall, ...] = ()


class LiveEvaluationFailureReport(StrictEvaluationModel):
    status: Literal["failed"] = "failed"
    failure: LiveEvaluationFailureEvidence


class LiveEvaluationProviderFailureEvidence(StrictEvaluationModel):
    """Sanitized context retained when a provider turn fails."""

    case_id: EvaluationIdentifier
    provider_call_index: int = Field(ge=1)
    elapsed_seconds: float = Field(ge=0)
    error_category: EvaluationIdentifier
    http_status_code: int | None = Field(default=None, ge=100, le=599)
    completed_provider_calls: int = Field(ge=0)
    completed_tool_rounds: int = Field(ge=0)
    completed_capability_executions: int = Field(ge=0)
    provider_call_evidence: tuple[RecordedProviderCall, ...] = ()
    tool_result_evidence: tuple[ProviderToolResult, ...] = ()


class LiveEvaluationProviderFailureReport(StrictEvaluationModel):
    status: Literal["failed"] = "failed"
    failure: LiveEvaluationProviderFailureEvidence


class LiveEvaluationProviderFailure(RuntimeError):
    """Safe evaluation-only wrapper for a normalized provider failure."""

    def __init__(self, evidence: LiveEvaluationProviderFailureEvidence) -> None:
        self.evidence = evidence
        super().__init__(f"live_evaluation_provider_failure:{evidence.error_category}")


class LiveEvaluationBudgetExceeded(RuntimeError):
    """Safe deterministic stop raised before activity can exceed a limit."""

    def __init__(
        self,
        budget: str,
        *,
        evidence: LiveEvaluationFailureEvidence | None = None,
    ) -> None:
        self.budget = budget
        self.evidence = evidence
        super().__init__(f"live_evaluation_budget_exceeded:{budget}")


class LiveEvaluationConfigurationError(RuntimeError):
    """Safe live-mode configuration failure without credential detail."""

    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(f"live_evaluation_configuration_error:{safe_code}")


class LiveEvaluationMatrixReport(StrictEvaluationModel):
    corpus_version: EvaluationIdentifier
    case_ids: tuple[EvaluationIdentifier, ...]
    reasoning_profiles: tuple[ProviderReasoningProfile, ...]
    budget: LiveEvaluationRunBudget
    reports: tuple[EvaluationReport, ...]


@dataclass
class _CaseBudgetState:
    provider_calls: int = 0
    tool_rounds: int = 0
    capability_executions: int = 0


class LiveEvaluationBudgetState:
    """Mutable counters shared across all profiles in one live matrix."""

    def __init__(
        self,
        budget: LiveEvaluationRunBudget,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self._clock = clock
        self._started_at = clock()
        self._case_executions = 0
        self._provider_calls = 0

    @property
    def clock(self) -> Callable[[], float]:
        return self._clock

    def prepare_run(self, cases: Sequence[EvaluationCase]) -> None:
        first_case_id = cases[0].case_id
        self._require_time(first_case_id)
        if self._case_executions + len(cases) > self.budget.max_case_executions:
            available = self.budget.max_case_executions - self._case_executions
            case_id = cases[max(0, available)].case_id
            self._raise_exceeded(
                case_id,
                "case_executions",
                self.budget.max_case_executions,
                self._case_executions + len(cases),
            )

    def start_case(self, case_id: EvaluationIdentifier) -> _CaseBudgetState:
        self._require_time(case_id)
        if self._case_executions >= self.budget.max_case_executions:
            self._raise_exceeded(
                case_id,
                "case_executions",
                self.budget.max_case_executions,
                self._case_executions + 1,
            )
        self._case_executions += 1
        return _CaseBudgetState()

    def reserve_provider_call(
        self, case_id: EvaluationIdentifier, case: _CaseBudgetState
    ) -> float:
        remaining = self._remaining_seconds(case_id, case)
        if case.provider_calls >= self.budget.max_provider_calls_per_case:
            self._raise_exceeded(
                case_id,
                "provider_calls_per_case",
                self.budget.max_provider_calls_per_case,
                case.provider_calls + 1,
                case,
            )
        if self._provider_calls >= self.budget.max_total_provider_calls:
            self._raise_exceeded(
                case_id,
                "total_provider_calls",
                self.budget.max_total_provider_calls,
                self._provider_calls + 1,
                case,
            )
        case.provider_calls += 1
        self._provider_calls += 1
        return min(float(self.budget.http_timeout_seconds), remaining)

    def reserve_tool_activity(
        self,
        case_id: EvaluationIdentifier,
        case: _CaseBudgetState,
        *,
        capability_calls: int,
    ) -> None:
        self._require_time(case_id, case)
        if case.tool_rounds >= self.budget.max_tool_rounds_per_case:
            self._raise_exceeded(
                case_id,
                "tool_rounds_per_case",
                self.budget.max_tool_rounds_per_case,
                case.tool_rounds + 1,
                case,
            )
        if (
            case.capability_executions + capability_calls
            > self.budget.max_capability_executions_per_case
        ):
            self._raise_exceeded(
                case_id,
                "capability_executions_per_case",
                self.budget.max_capability_executions_per_case,
                case.capability_executions + capability_calls,
                case,
            )
        case.tool_rounds += 1
        case.capability_executions += capability_calls

    def require_time(
        self, case_id: EvaluationIdentifier, case: _CaseBudgetState
    ) -> None:
        self._require_time(case_id, case)

    def _remaining_seconds(
        self,
        case_id: EvaluationIdentifier,
        case: _CaseBudgetState | None = None,
    ) -> float:
        elapsed = self._clock() - self._started_at
        remaining = self.budget.wall_clock_seconds - elapsed
        if remaining <= 0:
            self._raise_exceeded(
                case_id,
                "wall_clock",
                self.budget.wall_clock_seconds,
                elapsed,
                case,
            )
        return remaining

    def _require_time(
        self,
        case_id: EvaluationIdentifier,
        case: _CaseBudgetState | None = None,
    ) -> None:
        self._remaining_seconds(case_id, case)

    def _raise_exceeded(
        self,
        case_id: EvaluationIdentifier,
        budget: str,
        configured_limit: int | float,
        observed_value: int | float,
        case: _CaseBudgetState | None = None,
    ) -> None:
        raise LiveEvaluationBudgetExceeded(
            budget,
            evidence=LiveEvaluationFailureEvidence(
                case_id=case_id,
                exceeded_budget=budget,
                configured_limit=configured_limit,
                observed_value=observed_value,
                completed_provider_calls=case.provider_calls if case else 0,
                completed_tool_rounds=case.tool_rounds if case else 0,
                completed_capability_executions=(
                    case.capability_executions if case else 0
                ),
            ),
        )


class LiveEvaluationSource:
    """Execute normalized provider turns using only fictional case fixtures."""

    def __init__(
        self,
        adapter: ProviderTurnAdapter,
        *,
        reasoning_profile: ProviderReasoningProfile,
        budget_state: LiveEvaluationBudgetState,
        capability_registry: CapabilityRegistry = AI_CAPABILITY_REGISTRY,
        structured_commercial: bool = False,
    ) -> None:
        if not isinstance(reasoning_profile, ProviderReasoningProfile):
            raise LiveEvaluationConfigurationError("reasoning_profile_invalid")
        self._adapter = adapter
        self._reasoning_profile = reasoning_profile
        self._budget_state = budget_state
        self._capability_registry = capability_registry
        self._structured_commercial = structured_commercial

    def prepare_run(self, cases: Sequence[EvaluationCase]) -> None:
        self._budget_state.prepare_run(cases)

    async def observe(self, case: EvaluationCase) -> EvaluationObservation:
        case_budget = self._budget_state.start_case(case.case_id)
        policy = get_system_policy(
            _POLICY_LANGUAGE_BY_PATTERN[case.language_pattern],
            structured_commercial=self._structured_commercial,
        )
        specifications = self._capability_registry.specifications(
            case.exposed_capabilities
        )
        if {specification.name for specification in specifications} != set(
            case.exposed_capabilities
        ):
            raise LiveEvaluationConfigurationError("capability_not_registered")
        allowed_capabilities = tuple(
            ProviderCapability.from_specification(specification)
            for specification in specifications
        )
        messages = [
            ProviderMessage(role=item.role, content=item.content)
            for item in case.conversation_context
        ]
        messages.append(ProviderMessage(role="user", content=case.customer_input))
        fixture_by_name = {
            fixture.capability_name: fixture for fixture in case.capability_fixtures
        }
        if len(fixture_by_name) != len(case.capability_fixtures):
            raise LiveEvaluationConfigurationError("duplicate_capability_fixture")

        recorded_calls: list[RecordedProviderCall] = []
        tool_results: list[ProviderToolResult] = []
        continuation_state = None
        while True:
            try:
                timeout_seconds = self._budget_state.reserve_provider_call(
                    case.case_id, case_budget
                )
            except LiveEvaluationBudgetExceeded as exc:
                self._raise_with_partial_evidence(exc, recorded_calls, tool_results)
            request = ProviderTurnRequest(
                messages=tuple(messages),
                system_instruction=policy.text,
                allowed_capabilities=allowed_capabilities,
                max_output_tokens=(
                    self._budget_state.budget.max_output_tokens_per_call
                ),
                reasoning_profile=self._reasoning_profile,
                continuation_state=continuation_state,
            )
            started_at = self._budget_state.clock()
            try:
                result = await asyncio.wait_for(
                    self._adapter.generate_turn(request),
                    timeout=timeout_seconds,
                )
            except ProviderTurnError as exc:
                raise LiveEvaluationProviderFailure(
                    LiveEvaluationProviderFailureEvidence(
                        case_id=case.case_id,
                        provider_call_index=case_budget.provider_calls,
                        elapsed_seconds=max(
                            0.0,
                            self._budget_state.clock() - started_at,
                        ),
                        error_category=exc.safe_code,
                        completed_provider_calls=len(recorded_calls),
                        completed_tool_rounds=case_budget.tool_rounds,
                        completed_capability_executions=(
                            case_budget.capability_executions
                        ),
                        provider_call_evidence=tuple(recorded_calls),
                        tool_result_evidence=tuple(tool_results),
                    )
                ) from None
            except TimeoutError:
                try:
                    self._budget_state.require_time(case.case_id, case_budget)
                except LiveEvaluationBudgetExceeded as exc:
                    self._raise_with_partial_evidence(exc, recorded_calls, tool_results)
                raise LiveEvaluationBudgetExceeded(
                    "http_timeout",
                    evidence=LiveEvaluationFailureEvidence(
                        case_id=case.case_id,
                        exceeded_budget="http_timeout",
                        configured_limit=timeout_seconds,
                        observed_value=max(
                            timeout_seconds,
                            self._budget_state.clock() - started_at,
                        ),
                        completed_provider_calls=len(recorded_calls),
                        completed_tool_rounds=case_budget.tool_rounds,
                        completed_capability_executions=(
                            case_budget.capability_executions
                        ),
                        provider_call_evidence=tuple(recorded_calls),
                        tool_result_evidence=tuple(tool_results),
                    ),
                ) from None
            latency_ms = max(
                0,
                int((self._budget_state.clock() - started_at) * 1000),
            )
            recorded_calls.append(
                RecordedProviderCall(result=result, latency_ms=latency_ms)
            )
            try:
                self._budget_state.require_time(case.case_id, case_budget)
            except LiveEvaluationBudgetExceeded as exc:
                self._raise_with_partial_evidence(exc, recorded_calls, tool_results)

            if not result.tool_calls:
                final_outcome = (
                    EvaluationOutcomeClass.error
                    if result.finish_reason == ProviderFinishReason.error
                    else EvaluationOutcomeClass.answer
                )
                return EvaluationObservation(
                    case_id=case.case_id,
                    provider_calls=tuple(recorded_calls),
                    tool_results=tuple(tool_results),
                    final_outcome=final_outcome,
                )

            try:
                self._budget_state.reserve_tool_activity(
                    case.case_id,
                    case_budget,
                    capability_calls=len(result.tool_calls),
                )
            except LiveEvaluationBudgetExceeded as exc:
                if exc.budget not in {
                    "tool_rounds_per_case",
                    "capability_executions_per_case",
                }:
                    raise
                first_call = next(
                    (
                        call
                        for recorded in recorded_calls[:-1]
                        for call in recorded.result.tool_calls
                    ),
                    None,
                )
                raise LiveEvaluationBudgetExceeded(
                    exc.budget,
                    evidence=LiveEvaluationFailureEvidence(
                        case_id=case.case_id,
                        completed_provider_calls=case_budget.provider_calls,
                        completed_tool_rounds=case_budget.tool_rounds,
                        completed_capability_executions=(
                            case_budget.capability_executions
                        ),
                        provider_call_evidence=tuple(recorded_calls),
                        tool_result_evidence=tuple(tool_results),
                        first_tool_call=first_call,
                        first_tool_result=(tool_results[0] if tool_results else None),
                        rejected_tool_calls=result.tool_calls,
                        exceeded_budget=exc.budget,
                        configured_limit=(
                            self._budget_state.budget.max_tool_rounds_per_case
                            if exc.budget == "tool_rounds_per_case"
                            else self._budget_state.budget
                            .max_capability_executions_per_case
                        ),
                        observed_value=(
                            case_budget.tool_rounds + 1
                            if exc.budget == "tool_rounds_per_case"
                            else case_budget.capability_executions
                            + len(result.tool_calls)
                        ),
                    ),
                ) from None
            round_results = tuple(
                self._fixture_result(case, call, fixture_by_name)
                for call in result.tool_calls
            )
            tool_results.extend(round_results)
            if any(self._is_terminal_success(item) for item in round_results):
                return EvaluationObservation(
                    case_id=case.case_id,
                    provider_calls=tuple(recorded_calls),
                    tool_results=tuple(tool_results),
                    final_outcome=EvaluationOutcomeClass.handoff,
                )
            messages.extend(item.as_message() for item in round_results)
            continuation_state = result.continuation_state

    @staticmethod
    def _raise_with_partial_evidence(
        exc: LiveEvaluationBudgetExceeded,
        recorded_calls: Sequence[RecordedProviderCall],
        tool_results: Sequence[ProviderToolResult],
    ) -> None:
        if exc.evidence is None:
            raise exc
        raise LiveEvaluationBudgetExceeded(
            exc.budget,
            evidence=exc.evidence.model_copy(
                update={
                    "completed_provider_calls": len(recorded_calls),
                    "provider_call_evidence": tuple(recorded_calls),
                    "tool_result_evidence": tuple(tool_results),
                }
            ),
        ) from None

    def _fixture_result(
        self,
        case: EvaluationCase,
        call: ProviderToolCall,
        fixtures: dict[str, EvaluationCapabilityFixture],
    ) -> ProviderToolResult:
        if call.capability_name not in case.exposed_capabilities:
            return ProviderToolResult(
                call_id=call.call_id,
                capability_name=call.capability_name,
                status="error",
                error=ProviderToolError(
                    category="tool_not_allowed",
                    safe_code="capability_not_exposed",
                ),
            )
        fixture = fixtures.get(call.capability_name)
        if fixture is None:
            return ProviderToolResult(
                call_id=call.call_id,
                capability_name=call.capability_name,
                status="error",
                error=ProviderToolError(
                    category="execution_failed",
                    safe_code="evaluation_fixture_unavailable",
                ),
            )
        if fixture.status == "success":
            return ProviderToolResult(
                call_id=call.call_id,
                capability_name=call.capability_name,
                status="success",
                output=fixture.output,
            )
        return ProviderToolResult(
            call_id=call.call_id,
            capability_name=call.capability_name,
            status="error",
            error=ProviderToolError(
                category=fixture.error_category,
                safe_code=fixture.safe_code,
            ),
        )

    def _is_terminal_success(self, result: ProviderToolResult) -> bool:
        definition = self._capability_registry.resolve(result.capability_name)
        return bool(
            definition is not None
            and definition.terminal_on_success
            and result.status == "success"
        )
