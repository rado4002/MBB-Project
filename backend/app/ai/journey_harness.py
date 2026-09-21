"""Isolated evaluation harness that executes the production AI turn path.

The harness owns only deterministic fixtures and observation capture.  Turn
orchestration, authority checks, capability validation, terminal transactions,
audit persistence, and recovery remain owned by :class:`AITurnService` and M1.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import json
from typing import Any
import uuid

from app.adapters.base import ProviderTurnAdapter
from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityDefinition,
    CapabilityRegistry,
    SafeCapabilityError,
)
from app.ai.evaluation import (
    EvaluationCase,
    EvaluationObservation,
    EvaluationOutcomeClass,
    RecordedProviderCall,
)
from app.ai.provider_contract import (
    ProviderIdentity,
    ProviderToolError,
    ProviderToolResult,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from app.ai.turn import (
    AITurn,
    AITurnExecutionError,
    AITurnService,
    FinalizedAITurnResult,
)

_JOURNEY_NAMESPACE = uuid.UUID(
    "10000000-0000-4000-8000-000000000099"
)


class ScriptedProvider(ProviderTurnAdapter):
    """Deterministic adapter used by offline and disposable-DB journeys."""

    provider_name = "scripted"
    model = "production-journey-fixture"

    def __init__(
        self,
        results: Iterable[ProviderTurnResult | ProviderTurnError],
    ) -> None:
        self._results = deque(results)
        self.requests: list[ProviderTurnRequest] = []
        self.results: list[ProviderTurnResult] = []

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("unexpected scripted provider call")
        result = self._results.popleft()
        if isinstance(result, ProviderTurnError):
            raise result
        self.results.append(result)
        return result


def fixture_registry(
    fixtures: dict[str, object | Exception],
    *,
    base: CapabilityRegistry = AI_CAPABILITY_REGISTRY,
) -> CapabilityRegistry:
    """Provide deterministic results through the production executor contract.

    This replaces only handler dependencies.  Input validation, capability
    allow-listing, terminal handling, transactions, and audit behavior remain
    in the production registry/executor/service.
    """

    definitions: list[CapabilityDefinition] = []
    for name in fixtures:
        definition = base.resolve(name)
        if definition is None:
            raise ValueError(f"unknown production capability: {name}")
        fixture = fixtures[name]

        async def handler(_context: Any, _arguments: Any, *, value=fixture) -> object:
            if isinstance(value, Exception):
                raise SafeCapabilityError(str(value))
            return value

        async def transactional_handler(
            _session: Any,
            _context: Any,
            _arguments: Any,
            *,
            value=fixture,
        ) -> object:
            if isinstance(value, Exception):
                raise SafeCapabilityError(str(value))
            return value

        definitions.append(
            CapabilityDefinition(
                name=definition.name,
                description=definition.description,
                input_model=definition.input_model,
                output_model=definition.output_model,
                handler=(
                    None
                    if definition.transactional_handler is not None
                    else handler
                ),
                transactional_handler=(
                    transactional_handler
                    if definition.transactional_handler is not None
                    else None
                ),
                terminal_on_success=definition.terminal_on_success,
            )
        )
    return CapabilityRegistry(definitions)


@dataclass(frozen=True)
class JourneyExecution:
    case_id: str
    finalized: FinalizedAITurnResult | None
    execution_error: AITurnExecutionError | None
    provider_calls: tuple[ProviderTurnResult, ...]


class ProductionJourneyHarness:
    """Run one evaluation case through the real M1 AI turn service."""

    def __init__(
        self,
        provider: ScriptedProvider,
        *,
        service: AITurnService | None = None,
        service_factory: Callable[[ProviderTurnAdapter], AITurnService] | None = None,
    ) -> None:
        if service is not None and service_factory is not None:
            raise ValueError("provide service or service_factory, not both")
        self.provider = provider
        self.service = service or (
            service_factory(provider)
            if service_factory is not None
            else AITurnService(
                provider,
                provider_identity=ProviderIdentity(
                    provider=provider.provider_name,
                    model=provider.model,
                ),
            )
        )

    async def execute(self, case: EvaluationCase, turn: AITurn) -> JourneyExecution:
        try:
            finalized = await self.service.generate_finalized(turn)
        except AITurnExecutionError as exc:
            return JourneyExecution(
                case_id=case.case_id,
                finalized=None,
                execution_error=exc,
                provider_calls=tuple(self.provider.results),
            )
        return JourneyExecution(
            case_id=case.case_id,
            finalized=finalized,
            execution_error=None,
            provider_calls=tuple(self.provider.results),
        )

    async def observe(self, case: EvaluationCase, turn: AITurn) -> EvaluationObservation:
        execution = await self.execute(case, turn)
        calls = tuple(RecordedProviderCall(result=result) for result in execution.provider_calls)
        if not calls:
            raise ValueError("production journey produced no provider observation")
        audit = (
            execution.finalized.audit_record
            if execution.finalized is not None
            else execution.execution_error.audit_record
        )
        activities = list(audit.capability_activity)
        serialized_results: dict[str, dict[str, Any]] = {}
        for request in self.provider.requests:
            for message in request.messages:
                if message.role != "tool_result" or message.content is None:
                    continue
                try:
                    payload = json.loads(message.content)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict) and isinstance(payload.get("call_id"), str):
                    serialized_results[payload["call_id"]] = payload
        tool_results: list[ProviderToolResult] = []
        for provider_result in execution.provider_calls:
            for call in provider_result.tool_calls:
                activity = next(
                    (item for item in activities if item.capability_name == call.capability_name),
                    None,
                )
                payload = serialized_results.get(call.call_id)
                successful = bool(
                    payload is not None and payload.get("status") == "success"
                ) or (activity is not None and activity.outcome.value == "success")
                tool_results.append(
                    ProviderToolResult(
                        call_id=call.call_id,
                        capability_name=call.capability_name,
                        status="success" if successful else "error",
                        output=(
                            payload.get("output", {})
                            if successful and payload is not None
                            else ({} if successful else None)
                        ),
                        error=(
                            None
                            if successful
                            else ProviderToolError(category="execution_failed")
                        ),
                    )
                )
        if audit.outcome.value == "handoff_requested":
            final_outcome = EvaluationOutcomeClass.handoff
        elif audit.outcome.value == "order_draft_presented":
            final_outcome = EvaluationOutcomeClass.order_draft
        elif audit.outcome.value == "failed":
            final_outcome = EvaluationOutcomeClass.error
        else:
            final_outcome = EvaluationOutcomeClass.answer
        return EvaluationObservation(
            case_id=case.case_id,
            provider_calls=calls,
            tool_results=tuple(tool_results),
            final_outcome=final_outcome,
        )


class ProductionJourneyObservationSource:
    """Evaluation source backed by ``ProductionJourneyHarness``.

    The evaluator supplies the service factory so PostgreSQL tests can inject
    their disposable session/authority dependencies.  No provider or
    capability orchestration is implemented here.
    """

    def __init__(
        self,
        provider: ScriptedProvider,
        *,
        service_factory: Callable[[ProviderTurnAdapter], AITurnService],
    ) -> None:
        self._harness = ProductionJourneyHarness(
            provider,
            service_factory=service_factory,
        )

    async def observe(self, case: EvaluationCase):
        turn = AITurn(
            user_content=case.customer_input,
            language={
                "english": "french",
                "french": "french",
                "informal_french": "french",
                "french_lingala": "lingala",
                "french_swahili": "swahili",
            }[case.language_pattern.value],
            expected_ownership_version=1,
            conversation_id=uuid.uuid5(_JOURNEY_NAMESPACE, case.case_id),
            source_message_id=uuid.uuid5(
                _JOURNEY_NAMESPACE,
                f"source:{case.case_id}",
            ),
            history=tuple(
                {"role": item.role, "content": item.content}
                for item in case.conversation_context
            ),
            allowed_capabilities=tuple(case.exposed_capabilities),
        )
        return await self._harness.observe(case, turn)
