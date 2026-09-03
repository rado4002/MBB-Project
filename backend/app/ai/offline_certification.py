"""Deterministic, network-free support for the AI-5B1 certification harness."""

from __future__ import annotations

import asyncio
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Literal, Protocol, TypeVar

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


class EvaluationClock(Protocol):
    """Minimal clock/timer boundary used by evaluation-owned deadlines."""

    def monotonic(self) -> float:
        """Return the current evaluation time in seconds."""

    async def sleep(self, seconds: float) -> None:
        """Complete only after evaluation time advances by ``seconds``."""


class SystemEvaluationClock:
    """Real monotonic timer for a separately authorized evaluation run."""

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("evaluation sleep cannot be negative")
        await asyncio.sleep(seconds)


class ManualEvaluationClock:
    """Deterministic monotonic clock whose advancement wakes real async tasks."""

    def __init__(self, *, initial_seconds: float = 0.0) -> None:
        if initial_seconds < 0:
            raise ValueError("initial evaluation time cannot be negative")
        self._now = float(initial_seconds)
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    @property
    def pending_timer_count(self) -> int:
        return sum(not future.done() for _, future in self._waiters)

    async def sleep(self, seconds: float) -> None:
        if seconds < 0:
            raise ValueError("evaluation sleep cannot be negative")
        if seconds == 0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        waiter = (self._now + seconds, future)
        self._waiters.append(waiter)
        try:
            await future
        finally:
            if waiter in self._waiters:
                self._waiters.remove(waiter)

    async def advance(self, seconds: float) -> None:
        """Advance time and let every newly due async timer run."""
        if seconds < 0:
            raise ValueError("evaluation clock cannot move backward")
        self._now += seconds
        due = [
            future
            for deadline, future in tuple(self._waiters)
            if deadline <= self._now and not future.done()
        ]
        for future in due:
            future.set_result(None)
        await asyncio.sleep(0)
        await asyncio.sleep(0)


class OfflineDeadlineEvidence(_StrictModel):
    deadline_scope: Literal["provider_request"] = "provider_request"
    enforced_deadline_ms: int = Field(ge=1)
    virtual_started_ms: int = Field(ge=0)
    virtual_expired_ms: int = Field(ge=0)
    virtual_elapsed_ms: int = Field(ge=0)
    measured_wall_clock_ms: int = Field(ge=0)
    normalized_failure_code: Literal["timeout"] = "timeout"
    cancellation_requested: bool


class EvaluationDeadlineAdapter(ProviderTurnAdapter):
    """Enforce an evaluation-only deadline around each provider request."""

    def __init__(
        self,
        adapter: ProviderTurnAdapter,
        *,
        clock: EvaluationClock,
        deadline_seconds: float = AI5B1_PROVIDER_DEADLINE_SECONDS,
        wall_clock: Callable[[], float] = time.monotonic,
        on_timeout: Callable[[], None] | None = None,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("provider deadline must be positive")
        self._adapter = adapter
        self._clock = clock
        self._deadline_seconds = deadline_seconds
        self._wall_clock = wall_clock
        self._on_timeout = on_timeout
        self.provider_name = adapter.provider_name
        self.model = adapter.model
        self.deadline_evidence: list[OfflineDeadlineEvidence] = []
        self.accepted_results = 0
        self.late_completions_observed = 0
        self.late_completions_discarded = 0
        self.late_completion_finish_reasons: list[ProviderFinishReason] = []
        self._operations: set[asyncio.Task[ProviderTurnResult]] = set()
        self._late_monitors: set[asyncio.Task[None]] = set()

    @property
    def unfinished_task_count(self) -> int:
        return sum(
            not task.done() for task in (*self._operations, *self._late_monitors)
        )

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        virtual_started = self._clock.monotonic()
        wall_started = self._wall_clock()
        deadline = asyncio.create_task(self._clock.sleep(self._deadline_seconds))
        operation = asyncio.create_task(self._adapter.generate_turn(request))
        self._operations.add(operation)
        operation.add_done_callback(self._operations.discard)
        done, _pending = await asyncio.wait(
            (operation, deadline),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if deadline in done:
            cancellation_requested = operation.cancel()
            if self._on_timeout is not None:
                self._on_timeout()
            monitor = asyncio.create_task(self._observe_late_completion(operation))
            self._late_monitors.add(monitor)
            monitor.add_done_callback(self._late_monitors.discard)
            virtual_expired = self._clock.monotonic()
            self.deadline_evidence.append(
                OfflineDeadlineEvidence(
                    enforced_deadline_ms=round(self._deadline_seconds * 1_000),
                    virtual_started_ms=round(virtual_started * 1_000),
                    virtual_expired_ms=round(virtual_expired * 1_000),
                    virtual_elapsed_ms=round(
                        (virtual_expired - virtual_started) * 1_000
                    ),
                    measured_wall_clock_ms=max(
                        0, round((self._wall_clock() - wall_started) * 1_000)
                    ),
                    cancellation_requested=cancellation_requested,
                )
            )
            raise normalized_timeout()

        deadline.cancel()
        await asyncio.gather(deadline, return_exceptions=True)
        result = await operation
        self.accepted_results += 1
        return result

    async def _observe_late_completion(
        self, operation: asyncio.Task[ProviderTurnResult]
    ) -> None:
        try:
            result = await operation
        except asyncio.CancelledError:
            return
        except Exception:
            return
        self.late_completions_observed += 1
        self.late_completion_finish_reasons.append(result.finish_reason)
        self.late_completions_discarded += 1

    async def drain_late_completions(self) -> None:
        """Wait for observed late work so tests cannot leak background tasks."""
        monitors = tuple(self._late_monitors)
        if monitors:
            await asyncio.gather(*monitors, return_exceptions=True)


class OfflineWatchdogExpired(RuntimeError):
    """Raised when the evaluation-owned outer watchdog actually expires."""


class OfflineWatchdogEvidence(_StrictModel):
    enforced_watchdog_ms: int = Field(ge=1)
    virtual_elapsed_ms: int = Field(ge=0)
    measured_wall_clock_ms: int = Field(ge=0)
    stop_handler_called: bool
    cancellation_requested: bool


_WatchdogResult = TypeVar("_WatchdogResult")
WatchdogStopHandler = Callable[[], None | Awaitable[None]]


class EvaluationOuterWatchdog:
    """Stop one evaluation-owned operation when injected watchdog time expires."""

    def __init__(
        self,
        *,
        clock: EvaluationClock,
        stop_handler: WatchdogStopHandler,
        watchdog_seconds: float = AI5B1_OUTER_WATCHDOG_SECONDS,
        wall_clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if watchdog_seconds <= 0:
            raise ValueError("outer watchdog must be positive")
        self._clock = clock
        self._stop_handler = stop_handler
        self._watchdog_seconds = watchdog_seconds
        self._wall_clock = wall_clock
        self.evidence: list[OfflineWatchdogEvidence] = []
        self._tasks: set[asyncio.Task[object]] = set()

    @property
    def unfinished_task_count(self) -> int:
        return sum(not task.done() for task in self._tasks)

    async def run(self, operation: Awaitable[_WatchdogResult]) -> _WatchdogResult:
        virtual_started = self._clock.monotonic()
        wall_started = self._wall_clock()
        timer = asyncio.create_task(self._clock.sleep(self._watchdog_seconds))
        task = asyncio.create_task(operation)
        self._tasks.update((timer, task))
        timer.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._tasks.discard)
        done, _pending = await asyncio.wait(
            (task, timer),
            return_when=asyncio.FIRST_COMPLETED,
        )
        if timer in done:
            cancellation_requested = task.cancel()
            stop_result = self._stop_handler()
            if inspect.isawaitable(stop_result):
                await stop_result
            await asyncio.gather(task, return_exceptions=True)
            virtual_expired = self._clock.monotonic()
            self.evidence.append(
                OfflineWatchdogEvidence(
                    enforced_watchdog_ms=round(self._watchdog_seconds * 1_000),
                    virtual_elapsed_ms=round(
                        (virtual_expired - virtual_started) * 1_000
                    ),
                    measured_wall_clock_ms=max(
                        0, round((self._wall_clock() - wall_started) * 1_000)
                    ),
                    stop_handler_called=True,
                    cancellation_requested=cancellation_requested,
                )
            )
            raise OfflineWatchdogExpired("offline_evaluation_outer_watchdog_expired")

        timer.cancel()
        await asyncio.gather(timer, return_exceptions=True)
        return await task


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
        # Cumulative reservation totals are retained for evidence. Ceiling checks use
        # settled actuals plus unresolved reservations so the same request is never
        # counted twice.
        self.reserved_tokens = 0
        self.observed_tokens = 0
        self.reserved_cost_usd = Decimal("0")
        self.observed_cost_usd = Decimal("0")
        self.unresolved_reserved_tokens = 0
        self.unresolved_reserved_cost_usd = Decimal("0")
        self.durable_actions = 0
        self.reservation_violations = 0
        self._next_reservation_id = 1
        self._unresolved_reservations: dict[int, tuple[int, Decimal]] = {}

    @property
    def committed_tokens(self) -> int:
        return self.observed_tokens + self.unresolved_reserved_tokens

    @property
    def committed_cost_usd(self) -> Decimal:
        return self.observed_cost_usd + self.unresolved_reserved_cost_usd

    def reservation_is_unresolved(self, reservation_id: int) -> bool:
        return reservation_id in self._unresolved_reservations

    def reserve_provider_call(
        self,
        *,
        max_output_tokens: int,
        reserved_tokens: int,
        reserved_cost_usd: Decimal,
    ) -> int:
        if reserved_tokens < 0 or reserved_cost_usd < 0:
            raise ValueError("provider reservation cannot be negative")
        if max_output_tokens > self.limits.max_output_tokens_per_call:
            raise OfflineBudgetExceeded("max_output_tokens_per_call")
        if self.provider_calls + 1 > self.limits.max_provider_calls:
            raise OfflineBudgetExceeded("provider_calls")
        if self.committed_tokens + reserved_tokens > self.limits.max_total_tokens:
            raise OfflineBudgetExceeded("total_tokens")
        if (
            self.committed_cost_usd + reserved_cost_usd
            > self.limits.max_reserved_cost_usd
        ):
            raise OfflineBudgetExceeded("reserved_cost_usd")
        reservation_id = self._next_reservation_id
        self._next_reservation_id += 1
        self.provider_calls += 1
        self.reserved_tokens += reserved_tokens
        self.reserved_cost_usd += reserved_cost_usd
        self.unresolved_reserved_tokens += reserved_tokens
        self.unresolved_reserved_cost_usd += reserved_cost_usd
        self._unresolved_reservations[reservation_id] = (
            reserved_tokens,
            reserved_cost_usd,
        )
        return reservation_id

    def record_usage(
        self,
        usage: ProviderUsage | None,
        *,
        reservation_id: int | None = None,
        actual_cost_usd: Decimal | None = None,
    ) -> None:
        if usage is None or usage.total_tokens is None:
            return
        if reservation_id is None:
            try:
                reservation_id = next(iter(self._unresolved_reservations))
            except StopIteration:
                raise ValueError("no unresolved provider reservation") from None
        try:
            reserved_tokens, reserved_cost = self._unresolved_reservations[
                reservation_id
            ]
        except KeyError:
            raise ValueError("provider reservation is not unresolved") from None

        settled_cost = reserved_cost if actual_cost_usd is None else actual_cost_usd
        if settled_cost < 0:
            raise ValueError("settled provider cost cannot be negative")
        self._unresolved_reservations.pop(reservation_id)
        self.unresolved_reserved_tokens -= reserved_tokens
        self.unresolved_reserved_cost_usd -= reserved_cost
        self.observed_tokens += usage.total_tokens
        self.observed_cost_usd += settled_cost

        violation = usage.total_tokens > reserved_tokens or settled_cost > reserved_cost
        if violation:
            self.reservation_violations += 1
        if self.committed_tokens > self.limits.max_total_tokens:
            raise OfflineBudgetExceeded("observed_total_tokens")
        if self.committed_cost_usd > self.limits.max_reserved_cost_usd:
            raise OfflineBudgetExceeded("observed_cost_usd")
        if violation:
            raise OfflineBudgetExceeded("reservation_violation")

    def reserve_durable_action(self) -> None:
        if self.durable_actions + 1 > self.limits.max_durable_actions:
            raise OfflineBudgetExceeded("durable_actions")
        self.durable_actions += 1


class OfflineProviderCallEvidence(_StrictModel):
    call_index: int = Field(ge=1)
    requested_max_output_tokens: int = Field(ge=1, le=AI5B_MAX_OUTPUT_TOKENS)
    timing_basis: Literal["represented"] = "represented"
    represented_latency_ms: int = Field(ge=0)
    measured_wall_clock_ms: int = Field(ge=0)
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
        wall_started = time.monotonic()
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
                    represented_latency_ms=step.represented_latency_ms,
                    measured_wall_clock_ms=max(
                        0, round((time.monotonic() - wall_started) * 1_000)
                    ),
                    latency_class=classify_latency(step.represented_latency_ms),
                    failure_code=exc.safe_code,
                )
            )
            raise
        self.budget.record_usage(result.usage)
        usage = result.usage
        self.evidence.append(
            OfflineProviderCallEvidence(
                call_index=index + 1,
                requested_max_output_tokens=request.max_output_tokens,
                represented_latency_ms=step.represented_latency_ms,
                measured_wall_clock_ms=max(
                    0, round((time.monotonic() - wall_started) * 1_000)
                ),
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
