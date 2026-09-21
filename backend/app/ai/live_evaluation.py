"""Thin live controls around the production journey evaluation path.

This module owns authorization-adjacent budgets, provider selection evidence,
and stopping rules. Business execution remains in ``AITurnService`` through
``ProductionJourneyObservationSource``.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from typing import Literal, Protocol

from pydantic import Field

from app.adapters.base import ProviderTurnAdapter
from app.ai.evaluation import (
    EvaluationCase,
    EvaluationCorpus,
    EvaluationObservation,
    EvaluationReport,
    EvaluationRunMetadata,
    EvaluationRunner,
    StrictEvaluationModel,
)
from app.ai.provider_contract import (
    ProviderErrorCategory,
    ProviderIdentity,
    ProviderReasoningProfile,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
)

LIVE_JOURNEY_CASE_IDS = (
    "product.discovery.normal",
    "product.truth.current_price",
    "injection.pretend_stock",
    "language.french_lingala",
    "language.french_swahili",
)


class LiveEvaluationConfigurationError(RuntimeError):
    """Safe live-mode configuration failure without credential detail."""

    def __init__(self, safe_code: str) -> None:
        self.safe_code = safe_code
        super().__init__(f"live_evaluation_configuration_error:{safe_code}")


class LiveEvaluationRunBudget(StrictEvaluationModel):
    """Aggregate limits that can only tighten production turn ceilings."""

    max_case_executions: int = Field(default=20, ge=1, le=20)
    max_total_provider_calls: int = Field(default=40, ge=1, le=40)
    max_total_reserved_tokens: int = Field(default=40_000, ge=1, le=40_000)
    max_provider_calls_per_case: int = Field(default=2, ge=1, le=2)
    max_tool_rounds_per_case: int = Field(default=1, ge=1, le=1)
    max_capability_executions_per_case: int = Field(default=1, ge=1, le=1)
    wall_clock_seconds: int = Field(default=2700, ge=1, le=2700)
    http_timeout_seconds: float = Field(default=60, ge=0.001, le=60)
    transport_retries: Literal[0] = 0


class LiveEvaluationFailureEvidence(StrictEvaluationModel):
    reason: str
    case_executions_reserved: int = Field(ge=0)
    provider_calls_started: int = Field(ge=0)
    completed_provider_calls: int = Field(ge=0)
    reserved_tokens: int = Field(ge=0)


class LiveEvaluationFailureReport(StrictEvaluationModel):
    status: Literal["failed"] = "failed"
    failure: LiveEvaluationFailureEvidence


class LiveEvaluationBudgetExceeded(RuntimeError):
    """Deterministic stop raised before a live aggregate budget is exceeded."""

    def __init__(self, budget: str, evidence: LiveEvaluationFailureEvidence) -> None:
        self.budget = budget
        self.evidence = evidence
        super().__init__(f"live_evaluation_budget_exceeded:{budget}")


class LiveEvaluationProviderFailure(RuntimeError):
    """Sanitized provider failure with aggregate progress evidence."""

    def __init__(self, evidence: LiveEvaluationFailureEvidence) -> None:
        self.evidence = evidence
        super().__init__(f"live_evaluation_provider_failure:{evidence.reason}")


class LiveEvaluationMatrixReport(StrictEvaluationModel):
    corpus_version: str
    case_ids: tuple[str, ...]
    reasoning_profiles: tuple[ProviderReasoningProfile, ...]
    budget: LiveEvaluationRunBudget
    reports: tuple[EvaluationReport, ...]


class EvaluationObservationSource(Protocol):
    async def observe(self, case: EvaluationCase) -> EvaluationObservation: ...


LiveJourneySourceFactory = Callable[
    [ProviderTurnAdapter, ProviderReasoningProfile], EvaluationObservationSource
]


class _LiveBudgetState:
    def __init__(
        self,
        budget: LiveEvaluationRunBudget,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget = budget
        self.clock = clock
        self.started_at = clock()
        self.case_executions_reserved = 0
        self.provider_calls_started = 0
        self.completed_provider_calls = 0
        self.reserved_tokens = 0
        self.failure: RuntimeError | None = None

    def reserve_cases(self, count: int) -> None:
        self.require_time()
        if self.case_executions_reserved + count > self.budget.max_case_executions:
            self.raise_exceeded("case_executions")
        self.case_executions_reserved += count

    def reserve_provider_call(self, reserved_tokens: int) -> float:
        remaining = self.remaining_seconds()
        if self.provider_calls_started >= self.budget.max_total_provider_calls:
            self.raise_exceeded("total_provider_calls")
        if self.reserved_tokens + reserved_tokens > self.budget.max_total_reserved_tokens:
            self.raise_exceeded("total_reserved_tokens")
        self.provider_calls_started += 1
        self.reserved_tokens += reserved_tokens
        return min(float(self.budget.http_timeout_seconds), remaining)

    def complete_provider_call(self) -> None:
        self.completed_provider_calls += 1
        self.require_time()

    def remaining_seconds(self) -> float:
        remaining = self.budget.wall_clock_seconds - (self.clock() - self.started_at)
        if remaining <= 0:
            self.raise_exceeded("wall_clock")
        return remaining

    def require_time(self) -> None:
        self.remaining_seconds()

    def evidence(self, reason: str) -> LiveEvaluationFailureEvidence:
        return LiveEvaluationFailureEvidence(
            reason=reason,
            case_executions_reserved=self.case_executions_reserved,
            provider_calls_started=self.provider_calls_started,
            completed_provider_calls=self.completed_provider_calls,
            reserved_tokens=self.reserved_tokens,
        )

    def raise_exceeded(self, budget: str) -> None:
        failure = LiveEvaluationBudgetExceeded(budget, self.evidence(budget))
        self.failure = failure
        raise failure


def _structural_nodes(value: object) -> int:
    if isinstance(value, Mapping):
        return 1 + len(value) + sum(_structural_nodes(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return 1 + sum(_structural_nodes(item) for item in value)
    return 1


def _reserved_request_tokens(request: ProviderTurnRequest) -> int:
    """Conservatively reserve complete normalized request bytes plus structure."""
    payload: dict[str, object] = {
        "system_instruction": request.system_instruction,
        "messages": [item.model_dump(mode="json") for item in request.messages],
        "allowed_capabilities": [
            item.model_dump(mode="json") for item in request.allowed_capabilities
        ],
        "reasoning_profile": request.reasoning_profile.value,
    }
    if request.continuation_state is not None:
        payload["continuation_state"] = request.continuation_state.value
    serialized = json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8")
    return len(serialized) + _structural_nodes(payload) + request.max_output_tokens + 2


class BudgetedJourneyProvider(ProviderTurnAdapter):
    """Record calls and enforce live transport ceilings without orchestrating tools."""

    def __init__(self, adapter: ProviderTurnAdapter, state: _LiveBudgetState) -> None:
        self._adapter = adapter
        self._state = state
        self.requests: list[ProviderTurnRequest] = []
        self.results: list[ProviderTurnResult] = []

    @property
    def provider_identity(self) -> ProviderIdentity | None:
        return self._adapter.provider_identity

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        reserved_tokens = _reserved_request_tokens(request)
        timeout = self._state.reserve_provider_call(reserved_tokens)
        self.requests.append(request)
        try:
            result = await asyncio.wait_for(
                self._adapter.generate_turn(request),
                timeout=timeout,
            )
        except TimeoutError:
            failure = ProviderTurnError(ProviderErrorCategory.timeout)
            self._state.failure = failure
            raise failure from None
        self.results.append(result)
        self._state.complete_provider_call()
        if result.usage is not None and result.usage.total_tokens is not None:
            if result.usage.total_tokens > reserved_tokens:
                self._state.raise_exceeded("provider_token_reservation")
        return result


class _BudgetedObservationSource:
    def __init__(
        self,
        source: EvaluationObservationSource,
        state: _LiveBudgetState,
    ) -> None:
        self._source = source
        self._state = state

    def prepare_run(self, cases: Sequence[EvaluationCase]) -> None:
        self._state.reserve_cases(len(cases))

    async def observe(self, case: EvaluationCase) -> EvaluationObservation:
        self._state.require_time()
        observation = await self._source.observe(case)
        self._state.require_time()
        return observation


class LiveJourneyController:
    """Run authorized live observations through a production journey source."""

    def __init__(
        self,
        adapter: ProviderTurnAdapter,
        *,
        source_factory: LiveJourneySourceFactory,
        policy_version: str,
        budget: LiveEvaluationRunBudget = LiveEvaluationRunBudget(),
        explicitly_authorized: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if not explicitly_authorized:
            raise LiveEvaluationConfigurationError("live_authorization_required")
        identity = adapter.provider_identity
        if not isinstance(identity, ProviderIdentity) or identity.model is None:
            raise LiveEvaluationConfigurationError("provider_identity_unavailable")
        self._identity = identity
        self._source_factory = source_factory
        self._budget = budget
        self._state = _LiveBudgetState(budget, clock=clock)
        self._provider = BudgetedJourneyProvider(adapter, self._state)
        self._policy_version = policy_version

    async def run(
        self,
        corpus: EvaluationCorpus,
        *,
        case_ids: Sequence[str],
        reasoning_profiles: Sequence[ProviderReasoningProfile],
    ) -> LiveEvaluationMatrixReport:
        profiles = tuple(reasoning_profiles)
        selected_ids = tuple(case_ids)
        if not profiles or len(profiles) != len(set(profiles)):
            raise LiveEvaluationConfigurationError("reasoning_profiles_invalid")
        if not selected_ids or len(selected_ids) != len(set(selected_ids)):
            raise LiveEvaluationConfigurationError("case_selection_invalid")

        reports: list[EvaluationReport] = []
        try:
            for profile in profiles:
                source = _BudgetedObservationSource(
                    self._source_factory(self._provider, profile),
                    self._state,
                )
                runner = EvaluationRunner(
                    source,
                    EvaluationRunMetadata(
                        corpus_version=corpus.version,
                        provider=self._identity.provider,
                        model=self._identity.model,
                        reasoning_profile=profile,
                        policy_version=self._policy_version,
                    ),
                )
                report = await asyncio.wait_for(
                    runner.run(corpus, case_ids=selected_ids),
                    timeout=self._state.remaining_seconds(),
                )
                if self._state.failure is not None:
                    raise self._state.failure
                reports.append(report)
        except TimeoutError:
            self._state.raise_exceeded("wall_clock")
        except ProviderTurnError as exc:
            raise LiveEvaluationProviderFailure(
                self._state.evidence(exc.safe_code)
            ) from None

        return LiveEvaluationMatrixReport(
            corpus_version=corpus.version,
            case_ids=selected_ids,
            reasoning_profiles=profiles,
            budget=self._budget,
            reports=tuple(reports),
        )
