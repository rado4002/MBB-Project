"""Focused deterministic tests for AI-5B1 evaluation-owned time controls."""

from __future__ import annotations

import asyncio

import pytest

from app.ai.offline_certification import (
    AI5B1_OUTER_WATCHDOG_SECONDS,
    AI5B1_PROVIDER_DEADLINE_SECONDS,
    EvaluationDeadlineAdapter,
    EvaluationOuterWatchdog,
    ManualEvaluationClock,
    OfflineWatchdogExpired,
    RecordingScriptedProvider,
    ScriptedProviderStep,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderMessage,
    ProviderTurnRequest,
    ProviderTurnResult,
)


def _request() -> ProviderTurnRequest:
    return ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="Synthetic deadline probe"),),
        system_instruction="Synthetic evaluation-only policy.",
        max_output_tokens=512,
    )


async def _wait_for_timer(clock: ManualEvaluationClock) -> None:
    async def timer_is_registered() -> None:
        while clock.pending_timer_count == 0:
            await asyncio.sleep(0)

    await asyncio.wait_for(timer_is_registered(), timeout=1)


@pytest.mark.asyncio
async def test_provider_result_completed_before_deadline_is_accepted() -> None:
    clock = ManualEvaluationClock()
    started = asyncio.Event()
    release = asyncio.Event()

    async def complete_before_deadline(
        _request: ProviderTurnRequest,
    ) -> ProviderTurnResult:
        started.set()
        await release.wait()
        return ProviderTurnResult(
            text="Accepted synthetic result",
            finish_reason=ProviderFinishReason.completed,
        )

    scripted = RecordingScriptedProvider(
        (ScriptedProviderStep(complete_before_deadline),)
    )
    controller = EvaluationDeadlineAdapter(scripted, clock=clock)
    pending = asyncio.create_task(controller.generate_turn(_request()))
    await asyncio.wait_for(started.wait(), timeout=1)
    await _wait_for_timer(clock)

    await clock.advance(AI5B1_PROVIDER_DEADLINE_SECONDS - 0.001)
    assert not pending.done()
    release.set()
    result = await asyncio.wait_for(pending, timeout=1)

    assert result.text == "Accepted synthetic result"
    assert controller.accepted_results == 1
    assert controller.deadline_evidence == []
    assert controller.late_completions_observed == 0
    assert controller.unfinished_task_count == 0
    assert clock.pending_timer_count == 0


@pytest.mark.asyncio
async def test_outer_watchdog_expiry_invokes_stop_handler_and_drains_tasks() -> None:
    clock = ManualEvaluationClock()
    operation_started = asyncio.Event()
    operation_finished = asyncio.Event()
    stop_called = asyncio.Event()

    async def blocked_operation() -> None:
        operation_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            operation_finished.set()

    async def stop_handler() -> None:
        stop_called.set()

    watchdog = EvaluationOuterWatchdog(
        clock=clock,
        stop_handler=stop_handler,
    )
    supervised = asyncio.create_task(watchdog.run(blocked_operation()))
    await asyncio.wait_for(operation_started.wait(), timeout=1)
    await _wait_for_timer(clock)

    await clock.advance(AI5B1_OUTER_WATCHDOG_SECONDS - 0.001)
    assert not supervised.done()
    assert not stop_called.is_set()

    await clock.advance(0.001)
    with pytest.raises(OfflineWatchdogExpired):
        await asyncio.wait_for(supervised, timeout=1)

    assert stop_called.is_set()
    assert operation_finished.is_set()
    assert len(watchdog.evidence) == 1
    assert watchdog.evidence[0].virtual_elapsed_ms == 60_000
    assert watchdog.evidence[0].stop_handler_called is True
    assert watchdog.evidence[0].cancellation_requested is True
    assert watchdog.unfinished_task_count == 0
    assert clock.pending_timer_count == 0
