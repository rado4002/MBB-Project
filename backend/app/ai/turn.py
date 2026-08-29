"""Provider-neutral MBB AI turn contract and service."""
from __future__ import annotations

import json
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ProviderTurnAdapter
from app.ai.audit import (
    AITurnAuditRecord,
    AITurnOutcome,
    CapabilityAuditDecision,
    CapabilityAuditOutcome,
    CapabilityAuditSummary,
    CommercialStateField,
    append_ai_turn_audit,
)
from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityErrorCategory,
    CapabilityExecutionRuntime,
    CapabilityExecutionResult,
    CapabilityExecutor,
    CapabilityFailure,
    CapabilityRegistry,
    CapabilitySuccess,
    CapabilityTransactionRetry,
    RequestHumanHandoffOutput,
    TrustedCapabilityContext,
)
from app.ai.commercial_state import (
    COMMERCIAL_STATE_FINALIZER,
    CommercialState,
    CommercialStateProposal,
    CommercialStateUpdate,
    commercial_state_projection,
    read_commercial_state,
)
from app.ai.policy import get_system_policy
from app.ai.provider_contract import (
    ProviderCapability,
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderIdentity,
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
_MAX_DURABLE_ACTION_ATTEMPTS = 2

AuthorityChecker = Callable[[TrustedCapabilityContext], Awaitable[bool]]
DurableSessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
AuditAppender = Callable[[AsyncSession, AITurnAuditRecord], Awaitable[object]]
CommercialStateLoader = Callable[[uuid.UUID], Awaitable[CommercialState | None]]


class StaleAITurnAuthority(RuntimeError):
    """The trusted ownership generation no longer grants AI authority."""

    def __init__(self) -> None:
        super().__init__("ai_turn_authority_stale")


class AITurnBudgetExceeded(RuntimeError):
    """A fixed MBB orchestration budget was exhausted."""

    def __init__(self, budget: str) -> None:
        self.budget = budget
        super().__init__(f"ai_turn_budget_exceeded:{budget}")


class AITurnPersistenceError(RuntimeError):
    """A terminal AI action could not commit with its required audit."""

    def __init__(self) -> None:
        super().__init__("ai_turn_persistence_failed")


class AITurnExecutionError(RuntimeError):
    """Safe runtime failure carrying minimized provenance for M1 fallback."""

    def __init__(
        self,
        audit_record: AITurnAuditRecord,
        original_error: Exception,
    ) -> None:
        self.audit_record = audit_record
        self.original_error = original_error
        super().__init__(f"ai_turn_failed:{audit_record.safe_code or 'unknown'}")


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
    source_message_id: uuid.UUID | None = None
    history: Sequence[Mapping[str, str]] = ()
    allowed_capabilities: tuple[str, ...] = ()
    reasoning_profile: ProviderReasoningProfile = ProviderReasoningProfile.default
    turn_id: uuid.UUID = field(default_factory=uuid.uuid4, init=False)

    def __post_init__(self) -> None:
        if self.expected_ownership_version <= 0:
            raise ValueError("expected ownership version must be positive")
        if not isinstance(self.conversation_id, uuid.UUID):
            raise ValueError("conversation ID must be a UUID")
        if self.source_message_id is not None and not isinstance(
            self.source_message_id,
            uuid.UUID,
        ):
            raise ValueError("source message ID must be a UUID")
        if len(self.allowed_capabilities) != len(set(self.allowed_capabilities)):
            raise ValueError("allowed capability names must be unique")
        if not isinstance(self.reasoning_profile, ProviderReasoningProfile):
            raise ValueError("reasoning profile must be provider-neutral and typed")


@dataclass(frozen=True)
class FinalizedAITurnResult:
    """Customer text plus minimized, provider-neutral finalized provenance."""

    text: str | None
    audit_record: AITurnAuditRecord
    audit_persisted: bool = False
    commercial_state_snapshot_revision: int = 0
    commercial_state_update: CommercialStateUpdate | None = None
    outbound_message_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        is_handoff = self.audit_record.outcome == AITurnOutcome.handoff_requested
        if not is_handoff and self.text is None:
            raise ValueError("non-terminal finalized turn requires customer text")
        if is_handoff and (self.text is None) != (self.outbound_message_id is None):
            raise ValueError("terminal text and persisted outbound must appear together")
        if not is_handoff and self.outbound_message_id is not None:
            raise ValueError("normal turn cannot pre-persist its outbound message")
        if is_handoff != self.audit_persisted:
            raise ValueError("only terminal handoff audit is pre-persisted")
        if self.commercial_state_snapshot_revision < 0:
            raise ValueError("commercial-state snapshot revision cannot be negative")
        if is_handoff and self.commercial_state_update is not None:
            raise ValueError("terminal handoff cannot carry a commercial-state update")


class AITurnService:
    """Apply MBB policy and adapt an AI turn to the configured provider boundary."""

    def __init__(
        self,
        adapter: ProviderTurnAdapter,
        *,
        capability_registry: CapabilityRegistry = AI_CAPABILITY_REGISTRY,
        authority_checker: AuthorityChecker | None = None,
        limits: AITurnLimits = AITurnLimits(),
        durable_session_factory: DurableSessionFactory | None = None,
        audit_appender: AuditAppender | None = None,
        provider_identity: ProviderIdentity | None = None,
        commercial_state_loader: CommercialStateLoader | None = None,
    ) -> None:
        self._adapter = adapter
        self._capability_registry = capability_registry
        self._capability_executor = CapabilityExecutor(capability_registry)
        self._authority_checker = authority_checker
        self._limits = limits
        self._durable_session_factory = durable_session_factory
        self._audit_appender = audit_appender or append_ai_turn_audit
        self._commercial_state_loader = commercial_state_loader
        configured_identity = provider_identity
        if configured_identity is None:
            candidate = getattr(adapter, "provider_identity", None)
            if isinstance(candidate, ProviderIdentity):
                configured_identity = candidate
        self._provider_identity = configured_identity

    async def generate(self, turn: AITurn) -> str:
        """Compatibility text result for existing provider-turn callers."""
        try:
            finalized = await self.generate_finalized(turn)
        except AITurnExecutionError as exc:
            raise exc.original_error from None
        if finalized.text is None:
            raise StaleAITurnAuthority
        return finalized.text

    async def generate_finalized(self, turn: AITurn) -> FinalizedAITurnResult:
        """Run one bounded turn and finalize only minimized safe provenance."""
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
        if self._capability_registry.resolve(COMMERCIAL_STATE_FINALIZER) is not None:
            raise ProviderTurnError(ProviderErrorCategory.configuration)
        provider_capabilities = allowed_capabilities
        if self._commercial_state_loader is not None:
            provider_capabilities = (
                *allowed_capabilities,
                _commercial_state_finalizer_capability(),
            )
        tool_result_messages: list[ProviderMessage] = []
        capability_activity: list[CapabilityAuditSummary] = []
        seen_call_ids: set[str] = set()
        continuation_state = None
        provider_calls = 0
        tool_rounds = 0
        capability_executions = 0
        commercial_state: CommercialState | None = None
        commercial_state_revision = 0

        try:
            if self._commercial_state_loader is not None:
                commercial_state = await self._commercial_state_loader(
                    turn.conversation_id
                )
                commercial_state_revision = (
                    commercial_state.revision if commercial_state is not None else 0
                )
            base_messages = (
                ProviderMessage(
                    role="user",
                    content=_build_runtime_prompt(turn, commercial_state),
                ),
            )
            while True:
                if provider_calls >= self._limits.provider_calls:
                    raise AITurnBudgetExceeded("provider_calls")
                await self._require_current_authority(context)
                result = await self._adapter.generate_turn(
                    ProviderTurnRequest(
                        messages=base_messages + tuple(tool_result_messages),
                        system_instruction=policy.text,
                        allowed_capabilities=provider_capabilities,
                        max_output_tokens=_MAX_RESPONSE_TOKENS,
                        reasoning_profile=turn.reasoning_profile,
                        continuation_state=continuation_state,
                    )
                )
                provider_calls += 1

                finalizer_calls = tuple(
                    call
                    for call in result.tool_calls
                    if call.capability_name == COMMERCIAL_STATE_FINALIZER
                )
                if finalizer_calls:
                    if (
                        len(finalizer_calls) != 1
                        or len(result.tool_calls) != 1
                        or result.text is not None
                        or result.finish_reason != ProviderFinishReason.tool_call
                    ):
                        raise ProviderTurnError(
                            ProviderErrorCategory.malformed_response
                        )
                    try:
                        proposal = CommercialStateProposal.model_validate(
                            finalizer_calls[0].arguments
                        )
                    except Exception:
                        raise ProviderTurnError(
                            ProviderErrorCategory.malformed_response
                        ) from None
                    await self._require_current_authority(context)
                    return FinalizedAITurnResult(
                        text=proposal.response_text,
                        audit_record=self._audit_record(
                            turn=turn,
                            policy_version=policy.version,
                            exposed_capabilities=allowed_capabilities,
                            capability_activity=capability_activity,
                            outcome=AITurnOutcome.response_generated,
                            commercial_state_revision=commercial_state_revision,
                        ),
                        commercial_state_snapshot_revision=commercial_state_revision,
                        commercial_state_update=proposal.state_update,
                    )

                if not result.tool_calls:
                    if (
                        result.text is None
                        or result.finish_reason == ProviderFinishReason.tool_call
                    ):
                        raise ProviderTurnError(
                            ProviderErrorCategory.malformed_response
                        )
                    await self._require_current_authority(context)
                    return FinalizedAITurnResult(
                        text=result.text,
                        audit_record=self._audit_record(
                            turn=turn,
                            policy_version=policy.version,
                            exposed_capabilities=allowed_capabilities,
                            capability_activity=capability_activity,
                            outcome=AITurnOutcome.response_generated,
                            commercial_state_revision=commercial_state_revision,
                        ),
                        commercial_state_snapshot_revision=commercial_state_revision,
                    )

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
                for index, tool_call in enumerate(result.tool_calls):
                    await self._require_current_authority(context)
                    capability_executions += 1
                    definition = self._capability_registry.resolve(
                        tool_call.capability_name
                    )
                    if definition is not None and definition.terminal_on_success:
                        execution_result, terminal_result = (
                            await self._execute_terminal_capability(
                                tool_call=tool_call,
                                remaining_calls=result.tool_calls[index + 1 :],
                                turn=turn,
                                context=context,
                                policy_version=policy.version,
                                exposed_capabilities=allowed_capabilities,
                                prior_activity=capability_activity,
                                commercial_state_revision=commercial_state_revision,
                            )
                        )
                        if terminal_result is not None:
                            return terminal_result
                    else:
                        execution_result = await self._capability_executor.execute(
                            requested_name=tool_call.capability_name,
                            model_arguments=tool_call.arguments,
                            allowed_capabilities=turn.allowed_capabilities,
                            context=context,
                        )
                    capability_activity.append(
                        _capability_audit_summary(tool_call, execution_result)
                    )
                    tool_result_messages.append(
                        _provider_tool_result(tool_call, execution_result).as_message()
                    )

                continuation_state = result.continuation_state
        except AITurnPersistenceError:
            raise
        except Exception as exc:
            raise AITurnExecutionError(
                self._audit_record(
                    turn=turn,
                    policy_version=policy.version,
                    exposed_capabilities=allowed_capabilities,
                    capability_activity=capability_activity,
                    outcome=AITurnOutcome.failed,
                    safe_code=_turn_failure_safe_code(exc),
                    commercial_state_revision=commercial_state_revision,
                ),
                exc,
            ) from None

    async def _execute_terminal_capability(
        self,
        *,
        tool_call: ProviderToolCall,
        remaining_calls: Sequence[ProviderToolCall],
        turn: AITurn,
        context: TrustedCapabilityContext,
        policy_version: str,
        exposed_capabilities: Sequence[ProviderCapability],
        prior_activity: Sequence[CapabilityAuditSummary],
        commercial_state_revision: int,
    ) -> tuple[CapabilityExecutionResult, FinalizedAITurnResult | None]:
        if self._durable_session_factory is None:
            return (
                CapabilityFailure(
                    CapabilityErrorCategory.execution_failed,
                    safe_code="transaction_required",
                ),
                None,
            )

        for attempt in range(_MAX_DURABLE_ACTION_ATTEMPTS):
            async with self._durable_session_factory() as session:
                try:
                    if self._commercial_state_loader is not None:
                        await _require_current_transaction_snapshot(
                            session,
                            context=context,
                            source_message_id=turn.source_message_id,
                            commercial_state_revision=commercial_state_revision,
                        )
                    execution_result = await self._capability_executor.execute(
                        requested_name=tool_call.capability_name,
                        model_arguments=tool_call.arguments,
                        allowed_capabilities=turn.allowed_capabilities,
                        context=context,
                        runtime=CapabilityExecutionRuntime(
                            transaction_session=session
                        ),
                    )
                except CapabilityTransactionRetry:
                    await session.rollback()
                    if attempt + 1 < _MAX_DURABLE_ACTION_ATTEMPTS:
                        continue
                    return (
                        CapabilityFailure(
                            CapabilityErrorCategory.execution_failed,
                            safe_code="handoff_unavailable",
                        ),
                        None,
                    )

                if isinstance(execution_result, CapabilityFailure):
                    await session.rollback()
                    return execution_result, None

                activity = (
                    *prior_activity,
                    _capability_audit_summary(tool_call, execution_result),
                    *(
                        CapabilityAuditSummary(
                            capability_name=remaining.capability_name,
                            decision=CapabilityAuditDecision.requested,
                            outcome=CapabilityAuditOutcome.not_executed,
                        )
                        for remaining in remaining_calls
                    ),
                )
                audit_record = self._audit_record(
                    turn=turn,
                    policy_version=policy_version,
                    exposed_capabilities=exposed_capabilities,
                    capability_activity=activity,
                    outcome=AITurnOutcome.handoff_requested,
                    commercial_state_revision=commercial_state_revision,
                    commercial_state_revision_after=(
                        execution_result.output.commercial_state_revision_after
                        if isinstance(
                            execution_result.output,
                            RequestHumanHandoffOutput,
                        )
                        else None
                    ),
                    commercial_state_changed_fields=(
                        execution_result.output.commercial_state_changed_fields
                        if isinstance(
                            execution_result.output,
                            RequestHumanHandoffOutput,
                        )
                        else ()
                    ),
                    outbound_message_id=(
                        execution_result.output.outbound_message_id
                        if isinstance(
                            execution_result.output,
                            RequestHumanHandoffOutput,
                        )
                        else None
                    ),
                )
                try:
                    await self._audit_appender(session, audit_record)
                    await session.commit()
                except Exception:
                    await _rollback_quietly(session)
                    raise AITurnPersistenceError from None
                return (
                    execution_result,
                    FinalizedAITurnResult(
                        text=(
                            execution_result.output.acknowledgment_text
                            if isinstance(
                                execution_result.output,
                                RequestHumanHandoffOutput,
                            )
                            else None
                        ),
                        audit_record=audit_record,
                        audit_persisted=True,
                        commercial_state_snapshot_revision=commercial_state_revision,
                        outbound_message_id=(
                            execution_result.output.outbound_message_id
                            if isinstance(
                                execution_result.output,
                                RequestHumanHandoffOutput,
                            )
                            else None
                        ),
                    ),
                )

        raise AssertionError("durable action retry loop did not terminate")

    def _audit_record(
        self,
        *,
        turn: AITurn,
        policy_version: str,
        exposed_capabilities: Sequence[ProviderCapability],
        capability_activity: Sequence[CapabilityAuditSummary],
        outcome: AITurnOutcome,
        safe_code: str | None = None,
        commercial_state_revision: int = 0,
        commercial_state_revision_after: int | None = None,
        commercial_state_changed_fields: Sequence[str] = (),
        outbound_message_id: uuid.UUID | None = None,
    ) -> AITurnAuditRecord:
        identity = self._provider_identity
        return AITurnAuditRecord(
            turn_id=turn.turn_id,
            conversation_id=turn.conversation_id,
            source_message_id=turn.source_message_id,
            outbound_message_id=outbound_message_id,
            policy_version=policy_version,
            provider=identity.provider if identity is not None else None,
            model=identity.model if identity is not None else None,
            exposed_capabilities=tuple(
                capability.name for capability in exposed_capabilities
            ),
            capability_activity=tuple(capability_activity),
            commercial_state_revision_before=commercial_state_revision,
            commercial_state_revision_after=(
                commercial_state_revision
                if commercial_state_revision_after is None
                else commercial_state_revision_after
            ),
            commercial_state_changed_fields=tuple(
                CommercialStateField(field_name)
                for field_name in commercial_state_changed_fields
            ),
            outcome=outcome,
            safe_code=safe_code,
        )

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
    from app.database import async_session_factory

    return AITurnService(
        get_provider_turn_adapter(),
        authority_checker=_ai_authority_is_current,
        durable_session_factory=async_session_factory,
        commercial_state_loader=_postgres_commercial_state_loader,
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


async def _postgres_commercial_state_loader(
    conversation_id: uuid.UUID,
) -> CommercialState | None:
    """Read durable continuity once before inference without holding locks."""
    from app.database import async_session_factory

    async with async_session_factory() as session:
        return await read_commercial_state(session, conversation_id)


async def _require_current_transaction_snapshot(
    session: AsyncSession,
    *,
    context: TrustedCapabilityContext,
    source_message_id: uuid.UUID | None,
    commercial_state_revision: int,
) -> None:
    """Lock and reject a stale terminal result before any durable capability work."""
    from sqlalchemy import select

    from app.models.message import Message
    from app.modules.m4_conversation.ownership import ai_may_reply

    if not await ai_may_reply(
        session,
        context.conversation_id,
        lock=True,
        expected_ownership_version=context.expected_ownership_version,
    ):
        raise StaleAITurnAuthority
    current_state = await read_commercial_state(session, context.conversation_id)
    current_revision = current_state.revision if current_state is not None else 0
    if current_revision != commercial_state_revision:
        raise StaleAITurnAuthority
    if source_message_id is None:
        return
    latest_inbound_id = await session.scalar(
        select(Message.message_id)
        .where(
            Message.conversation_id == context.conversation_id,
            Message.direction == "inbound",
        )
        .order_by(
            Message.created_at.desc(),
            Message.timestamp.desc(),
            Message.message_id.desc(),
        )
        .limit(1)
    )
    if latest_inbound_id != source_message_id:
        raise StaleAITurnAuthority


async def _rollback_quietly(session: AsyncSession) -> None:
    try:
        await session.rollback()
    except Exception:
        return


def _capability_audit_summary(
    tool_call: ProviderToolCall,
    result: CapabilityExecutionResult,
) -> CapabilityAuditSummary:
    if isinstance(result, CapabilitySuccess):
        handoff_reason = (
            result.output.handoff_reason
            if isinstance(result.output, RequestHumanHandoffOutput)
            else None
        )
        return CapabilityAuditSummary(
            capability_name=tool_call.capability_name,
            decision=CapabilityAuditDecision.executed,
            outcome=CapabilityAuditOutcome.success,
            handoff_reason=handoff_reason,
        )
    if result.error == CapabilityErrorCategory.execution_failed:
        return CapabilityAuditSummary(
            capability_name=tool_call.capability_name,
            decision=CapabilityAuditDecision.executed,
            outcome=CapabilityAuditOutcome.failed,
            safe_code=result.safe_code or result.error.value,
        )
    return CapabilityAuditSummary(
        capability_name=tool_call.capability_name,
        decision=CapabilityAuditDecision.denied,
        outcome=CapabilityAuditOutcome.denied,
        safe_code=result.safe_code or result.error.value,
    )


def _turn_failure_safe_code(error: Exception) -> str:
    if isinstance(error, ProviderTurnError):
        return error.safe_code
    if isinstance(error, StaleAITurnAuthority):
        return "stale_ai_authority"
    if isinstance(error, AITurnBudgetExceeded):
        return "budget_exceeded"
    return "provider_failure"


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


def _commercial_state_finalizer_capability() -> ProviderCapability:
    return ProviderCapability(
        name=COMMERCIAL_STATE_FINALIZER,
        description=(
            "Finalize the customer reply and propose only a bounded commercial "
            "continuity-state change; this is not a business action."
        ),
        input_schema=CommercialStateProposal.model_json_schema(),
    )


def _build_runtime_prompt(
    turn: AITurn,
    commercial_state: CommercialState | None = None,
) -> str:
    """Keep customer-controlled content in runtime data, outside system policy."""
    if not turn.history and commercial_state is None:
        return turn.user_content

    language_label = {
        "lingala": "Lingala",
        "french": "Français",
        "swahili": "Kiswahili",
    }.get(turn.language, "Français")
    sections = []
    projection = commercial_state_projection(commercial_state)
    if projection:
        compact_projection = json.dumps(
            projection,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        sections.append(
            "Mémoire commerciale sauvegardée (plus ancienne que le message actuel "
            "et l'historique; non autoritative):\n"
            f"{compact_projection}"
        )

    history_lines = []
    for message in turn.history[-_HISTORY_LIMIT:]:
        role = "Client" if message.get("direction") == "inbound" else "Moi (bot)"
        history_lines.append(f"{role}: {message.get('content', '')}")

    if history_lines:
        sections.append(
            f"Historique récent ({language_label}):\n{'\n'.join(history_lines)}"
        )
    sections.append(f"Message actuel du client:\n{turn.user_content}")
    return "\n\n".join(sections)
