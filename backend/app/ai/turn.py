"""Provider-neutral MBB AI turn contract and service."""
from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from app.adapters.base import ProviderTurnAdapter
from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityExecutionResult,
    CapabilityExecutor,
    CapabilityFailure,
    CapabilityRegistry,
    TrustedCapabilityContext,
)
from app.ai.policy import get_system_policy
from app.ai.provider_contract import (
    ProviderCapability,
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderToolError,
    ProviderToolResult,
    ProviderTurnError,
    ProviderTurnRequest,
)

_MAX_RESPONSE_TOKENS = 512
_HISTORY_LIMIT = 6
_MAX_PROVIDER_CALLS = 3
_MAX_TOOL_ROUNDS = 2
_MAX_CAPABILITY_EXECUTIONS = 3

AuthorityChecker = Callable[[TrustedCapabilityContext], Awaitable[bool]]


class StaleAITurnAuthority(RuntimeError):
    """The trusted ownership generation no longer grants AI authority."""

    def __init__(self) -> None:
        super().__init__("ai_turn_authority_stale")


class AITurnBudgetExceeded(RuntimeError):
    """A fixed MBB orchestration budget was exhausted."""

    def __init__(self, budget: str) -> None:
        self.budget = budget
        super().__init__(f"ai_turn_budget_exceeded:{budget}")


@dataclass(frozen=True)
class AITurnLimits:
    """Per-turn limits that may only tighten the permanent MBB ceilings."""

    provider_calls: int = _MAX_PROVIDER_CALLS
    tool_rounds: int = _MAX_TOOL_ROUNDS
    capability_executions: int = _MAX_CAPABILITY_EXECUTIONS

    def __post_init__(self) -> None:
        ceilings = (
            ("provider calls", self.provider_calls, _MAX_PROVIDER_CALLS),
            ("tool rounds", self.tool_rounds, _MAX_TOOL_ROUNDS),
            (
                "capability executions",
                self.capability_executions,
                _MAX_CAPABILITY_EXECUTIONS,
            ),
        )
        if any(value <= 0 or value > ceiling for _, value, ceiling in ceilings):
            raise ValueError("AI turn limits must be positive and within hard ceilings")


@dataclass(frozen=True)
class AITurn:
    """The runtime context MBB currently needs for one assistant reply."""

    user_content: str
    language: str
    expected_ownership_version: int
    conversation_id: uuid.UUID
    history: Sequence[Mapping[str, str]] = ()
    allowed_capabilities: tuple[str, ...] = ()
    turn_id: uuid.UUID = field(default_factory=uuid.uuid4, init=False)

    def __post_init__(self) -> None:
        if self.expected_ownership_version <= 0:
            raise ValueError("expected ownership version must be positive")
        if not isinstance(self.conversation_id, uuid.UUID):
            raise ValueError("conversation ID must be a UUID")
        if len(self.allowed_capabilities) != len(set(self.allowed_capabilities)):
            raise ValueError("allowed capability names must be unique")


class AITurnService:
    """Apply MBB policy and adapt an AI turn to the configured provider boundary."""

    def __init__(
        self,
        adapter: ProviderTurnAdapter,
        *,
        capability_registry: CapabilityRegistry = AI_CAPABILITY_REGISTRY,
        authority_checker: AuthorityChecker | None = None,
        limits: AITurnLimits = AITurnLimits(),
    ) -> None:
        self._adapter = adapter
        self._capability_registry = capability_registry
        self._capability_executor = CapabilityExecutor(capability_registry)
        self._authority_checker = authority_checker
        self._limits = limits

    async def generate(self, turn: AITurn) -> str:
        policy = get_system_policy(turn.language)
        context = TrustedCapabilityContext(
            conversation_id=turn.conversation_id,
            turn_id=turn.turn_id,
            expected_ownership_version=turn.expected_ownership_version,
        )
        if turn.allowed_capabilities and self._authority_checker is None:
            raise ProviderTurnError(ProviderErrorCategory.configuration)

        allowed_capabilities = tuple(
            ProviderCapability.from_specification(specification)
            for specification in self._capability_registry.specifications(
                turn.allowed_capabilities
            )
        )
        base_messages = (
            ProviderMessage(role="user", content=_build_runtime_prompt(turn)),
        )
        tool_result_messages: list[ProviderMessage] = []
        seen_call_ids: set[str] = set()
        continuation_state = None
        provider_calls = 0
        tool_rounds = 0
        capability_executions = 0

        while True:
            if provider_calls >= self._limits.provider_calls:
                raise AITurnBudgetExceeded("provider_calls")
            await self._require_current_authority(context)
            result = await self._adapter.generate_turn(
                ProviderTurnRequest(
                    messages=base_messages + tuple(tool_result_messages),
                    system_instruction=policy.text,
                    allowed_capabilities=allowed_capabilities,
                    max_output_tokens=_MAX_RESPONSE_TOKENS,
                    reasoning_profile=ProviderReasoningProfile.default,
                    continuation_state=continuation_state,
                )
            )
            provider_calls += 1

            if not result.tool_calls:
                if (
                    result.text is None
                    or result.finish_reason == ProviderFinishReason.tool_call
                ):
                    raise ProviderTurnError(ProviderErrorCategory.malformed_response)
                await self._require_current_authority(context)
                return result.text

            if result.finish_reason != ProviderFinishReason.tool_call:
                raise ProviderTurnError(ProviderErrorCategory.malformed_response)
            if tool_rounds >= self._limits.tool_rounds:
                raise AITurnBudgetExceeded("tool_rounds")
            tool_rounds += 1

            round_call_ids = {call.call_id for call in result.tool_calls}
            if (
                len(round_call_ids) != len(result.tool_calls)
                or round_call_ids.intersection(seen_call_ids)
            ):
                raise ProviderTurnError(ProviderErrorCategory.malformed_response)
            seen_call_ids.update(round_call_ids)

            if (
                capability_executions + len(result.tool_calls)
                > self._limits.capability_executions
            ):
                raise AITurnBudgetExceeded("capability_executions")
            for tool_call in result.tool_calls:
                await self._require_current_authority(context)
                capability_executions += 1
                execution_result = await self._capability_executor.execute(
                    requested_name=tool_call.capability_name,
                    model_arguments=tool_call.arguments,
                    allowed_capabilities=turn.allowed_capabilities,
                    context=context,
                )
                tool_result_messages.append(
                    _provider_tool_result(tool_call, execution_result).as_message()
                )

            continuation_state = result.continuation_state

    async def _require_current_authority(
        self,
        context: TrustedCapabilityContext,
    ) -> None:
        if self._authority_checker is None:
            return
        try:
            is_current = await self._authority_checker(context)
        except Exception:
            raise StaleAITurnAuthority from None
        if not is_current:
            raise StaleAITurnAuthority


def get_ai_turn_service() -> AITurnService:
    """Build the service using the repository's existing adapter factory."""
    from app.adapters import get_provider_turn_adapter

    return AITurnService(
        get_provider_turn_adapter(),
        authority_checker=_ai_authority_is_current,
    )


async def _ai_authority_is_current(context: TrustedCapabilityContext) -> bool:
    """Re-check the existing authoritative ownership/version gate."""
    from app.database import async_session_factory
    from app.modules.m4_conversation.ownership import ai_may_reply

    async with async_session_factory() as session:
        return await ai_may_reply(
            session,
            context.conversation_id,
            expected_ownership_version=context.expected_ownership_version,
        )


def _provider_tool_result(
    tool_call: ProviderToolCall,
    result: CapabilityExecutionResult,
) -> ProviderToolResult:
    if isinstance(result, CapabilityFailure):
        return ProviderToolResult(
            call_id=tool_call.call_id,
            capability_name=tool_call.capability_name,
            status="error",
            error=ProviderToolError(
                category=result.error.value,
                safe_code=result.safe_code,
            ),
        )
    return ProviderToolResult(
        call_id=tool_call.call_id,
        capability_name=tool_call.capability_name,
        status="success",
        output=result.output.model_dump(mode="json"),
    )


def _build_runtime_prompt(turn: AITurn) -> str:
    """Keep customer-controlled content in runtime data, outside system policy."""
    if not turn.history:
        return turn.user_content

    language_label = {
        "lingala": "Lingala",
        "french": "Français",
        "swahili": "Kiswahili",
    }.get(turn.language, "Français")
    history_lines = []
    for message in turn.history[-_HISTORY_LIMIT:]:
        role = "Client" if message.get("direction") == "inbound" else "Moi (bot)"
        history_lines.append(f"{role}: {message.get('content', '')}")

    return (
        f"Historique récent ({language_label}):\n"
        f"{'\n'.join(history_lines)}\n\n"
        f"Message actuel du client:\n{turn.user_content}"
    )
