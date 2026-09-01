"""Deterministic, network-free support for the AI-5B1 certification harness."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.base import ProviderTurnAdapter
from app.ai.provider_contract import (
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderUsage,
)

AI5B_CONTRACT_VERSION = "mbb-ai5b-contract-v2"
AI5B1_PROVIDER_DEADLINE_SECONDS = 12
AI5B1_SAFE_BOUNDARY_SECONDS = 15
AI5B1_OUTER_WATCHDOG_SECONDS = 60
AI5B_MAX_OUTPUT_TOKENS = 512


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OfflineLatencyClass(str, Enum):
    one_call_target = "one_call_target"
    routine_target = "routine_target"
    below_warning = "below_warning"
    warning = "warning"
    provider_deadline = "provider_deadline"
    safe_boundary = "safe_boundary"
    outer_watchdog = "outer_watchdog"
    beyond_watchdog = "beyond_watchdog"


def classify_latency(milliseconds: int) -> OfflineLatencyClass:
    """Classify synthetic or measured latency without implying a live sample."""
    if milliseconds < 0:
        raise ValueError("latency cannot be negative")
    if milliseconds <= 5_000:
        return OfflineLatencyClass.one_call_target
    if milliseconds <= 6_000:
        return OfflineLatencyClass.routine_target
    if milliseconds < 10_000:
        return OfflineLatencyClass.below_warning
    if milliseconds < 12_000:
        return OfflineLatencyClass.warning
    if milliseconds == 12_000:
        return OfflineLatencyClass.provider_deadline
    if milliseconds <= 15_000:
        return OfflineLatencyClass.safe_boundary
    if milliseconds <= 60_000:
        return OfflineLatencyClass.outer_watchdog
    return OfflineLatencyClass.beyond_watchdog


class OfflineBudgetLimits(_StrictModel):
    max_provider_calls: int = Field(default=21, ge=1)
    max_total_tokens: int = Field(default=40_000, ge=1)
    max_reserved_cost_usd: Decimal = Field(default=Decimal("0.05"), ge=0)
    max_durable_actions: int = Field(default=1, ge=0)
    max_output_tokens_per_call: int = Field(default=AI5B_MAX_OUTPUT_TOKENS, ge=1)


class OfflineBudgetExceeded(RuntimeError):
    """Raised before an offline action can exceed a configured ceiling."""

    def __init__(self, budget: str) -> None:
        self.budget = budget
        super().__init__(f"offline_certification_budget_exceeded:{budget}")


class OfflineBudgetLedger:
    """Reserve ceilings before calls/actions and record synthetic usage afterward."""

    def __init__(self, limits: OfflineBudgetLimits = OfflineBudgetLimits()) -> None:
        self.limits = limits
        self.provider_calls = 0
        self.reserved_tokens = 0
        self.observed_tokens = 0
        self.reserved_cost_usd = Decimal("0")
        self.durable_actions = 0

    def reserve_provider_call(
        self,
        *,
        max_output_tokens: int,
        reserved_tokens: int,
        reserved_cost_usd: Decimal,
    ) -> None:
        if max_output_tokens > self.limits.max_output_tokens_per_call:
            raise OfflineBudgetExceeded("max_output_tokens_per_call")
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            raise OfflineBudgetExceeded("provider_calls")
        if self.reserved_tokens + reserved_tokens > self.limits.max_total_tokens:
            raise OfflineBudgetExceeded("total_tokens")
        if (
            self.reserved_cost_usd + reserved_cost_usd
            > self.limits.max_reserved_cost_usd
        ):
            raise OfflineBudgetExceeded("reserved_cost_usd")
        self.provider_calls += 1
        self.reserved_tokens += reserved_tokens
        self.reserved_cost_usd += reserved_cost_usd

    def record_usage(self, usage: ProviderUsage | None) -> None:
        if usage is None or usage.total_tokens is None:
            return
        if self.observed_tokens + usage.total_tokens > self.limits.max_total_tokens:
            raise OfflineBudgetExceeded("observed_total_tokens")
        self.observed_tokens += usage.total_tokens

    def reserve_durable_action(self) -> None:
        if self.durable_actions + 1 > self.limits.max_durable_actions:
            raise OfflineBudgetExceeded("durable_actions")
        self.durable_actions += 1


class OfflineProviderCallEvidence(_StrictModel):
    call_index: int = Field(ge=1)
    requested_max_output_tokens: int = Field(ge=1, le=AI5B_MAX_OUTPUT_TOKENS)
    latency_ms: int = Field(ge=0)
    latency_class: OfflineLatencyClass
    finish_reason: ProviderFinishReason | None = None
    failure_code: str | None = None
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)
    provider_network_calls: int = Field(default=0, ge=0, le=0)
    provider_api_tokens: int = Field(default=0, ge=0, le=0)
    provider_cost_usd: Decimal = Field(default=Decimal("0"), ge=0, le=0)


ProviderStepAction = Callable[
    [ProviderTurnRequest],
    ProviderTurnResult | Awaitable[ProviderTurnResult],
]


@dataclass(frozen=True)
class ScriptedProviderStep:
    action: ProviderStepAction
    represented_latency_ms: int = 0
    reserved_tokens: int = 0
    reserved_cost_usd: Decimal = Decimal("0")
    late_result: ProviderTurnResult | None = None

    def __post_init__(self) -> None:
        if self.represented_latency_ms < 0 or self.reserved_tokens < 0:
            raise ValueError("scripted provider reservations cannot be negative")
        if self.reserved_cost_usd < 0:
            raise ValueError("scripted provider cost reservation cannot be negative")


class RecordingScriptedProvider(ProviderTurnAdapter):
    """Fixed provider script with fail-closed budgets and no network capability."""

    provider_name = "scripted"
    model = "offline-ai5b1"

    def __init__(
        self,
        steps: Sequence[ScriptedProviderStep],
        *,
        budget: OfflineBudgetLedger | None = None,
    ) -> None:
        self._steps = tuple(steps)
        self.budget = budget or OfflineBudgetLedger()
        self.requests: list[ProviderTurnRequest] = []
        self.evidence: list[OfflineProviderCallEvidence] = []
        self.rejected_late_results = 0
        self.network_calls = 0

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        index = len(self.requests)
        if index >= len(self._steps):
            raise AssertionError("scripted provider exceeded its fixed sequence")
        step = self._steps[index]
        self.budget.reserve_provider_call(
            max_output_tokens=request.max_output_tokens,
            reserved_tokens=step.reserved_tokens,
            reserved_cost_usd=step.reserved_cost_usd,
        )
        self.requests.append(request)
        try:
            result = step.action(request)
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, ProviderTurnResult):
                raise TypeError("scripted provider step returned an invalid result")
        except ProviderTurnError as exc:
            self.evidence.append(
                OfflineProviderCallEvidence(
                    call_index=index + 1,
                    requested_max_output_tokens=request.max_output_tokens,
                    latency_ms=step.represented_latency_ms,
                    latency_class=classify_latency(step.represented_latency_ms),
                    failure_code=exc.safe_code,
                )
            )
            if step.late_result is not None:
                self.rejected_late_results += 1
            raise
        self.budget.record_usage(result.usage)
        usage = result.usage
        self.evidence.append(
            OfflineProviderCallEvidence(
                call_index=index + 1,
                requested_max_output_tokens=request.max_output_tokens,
                latency_ms=step.represented_latency_ms,
                latency_class=classify_latency(step.represented_latency_ms),
                finish_reason=result.finish_reason,
                input_tokens=usage.input_tokens if usage else None,
                output_tokens=usage.output_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                cache_hit_tokens=usage.cache_hit_tokens if usage else None,
                cache_miss_tokens=usage.cache_miss_tokens if usage else None,
                reasoning_tokens=usage.reasoning_tokens if usage else None,
            )
        )
        return result


_REDACTED_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "authorization_header",
        "continuation_state",
        "credential",
        "credentials",
        "hidden_chain_of_thought",
        "password",
        "provider_continuation",
        "reasoning_content",
        "secret",
        "token",
    }
)


def redact_evidence(value: object) -> object:
    """Remove secret/hidden-reasoning fields while preserving synthetic Unicode."""
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): redact_evidence(item)
            for key, item in value.items()
            if str(key).lower() not in _REDACTED_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [redact_evidence(item) for item in value]
    return value


def redacted_evidence_json(value: object) -> str:
    """Serialize redacted synthetic evidence without ASCII-loss escaping."""
    return json.dumps(
        redact_evidence(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def normalized_timeout() -> ProviderTurnError:
    """Create the same provider-neutral timeout consumed by the real MBB path."""
    return ProviderTurnError(ProviderErrorCategory.timeout)
