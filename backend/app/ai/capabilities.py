"""Strict provider-neutral boundary for future MBB AI capabilities."""
from __future__ import annotations

import re
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

_CAPABILITY_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_SAFE_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_MAX_DESCRIPTION_LENGTH = 200
_MODEL_FORBIDDEN_ARGUMENTS = frozenset(
    {
        "allowed_tools",
        "conversation_id",
        "turn_id",
        "ownership_version",
        "expected_ownership_version",
        "human_owner_account_id",
        "owner_type",
        "customer_id",
        "business_id",
        "tenant_id",
        "actor_id",
        "permissions",
        "owner_id",
        "internal_account_id",
    }
)


class StrictCapabilityModel(BaseModel):
    """Base contract for validated capability inputs and safe outputs."""

    model_config = ConfigDict(extra="forbid", strict=True)


@dataclass(frozen=True)
class TrustedCapabilityContext:
    """MBB-supplied business scope that model arguments cannot modify."""

    conversation_id: uuid.UUID
    turn_id: uuid.UUID
    expected_ownership_version: int

    def __post_init__(self) -> None:
        if self.expected_ownership_version <= 0:
            raise ValueError("expected ownership version must be positive")


CapabilityHandler = Callable[
    [TrustedCapabilityContext, StrictCapabilityModel],
    Awaitable[object],
]


@dataclass(frozen=True)
class CapabilityDefinition:
    name: str
    description: str
    input_model: type[StrictCapabilityModel]
    output_model: type[StrictCapabilityModel]
    handler: CapabilityHandler

    def __post_init__(self) -> None:
        if not _CAPABILITY_NAME_PATTERN.fullmatch(self.name):
            raise ValueError("capability name must be stable lower_snake_case")
        description = self.description.strip()
        if not description or len(description) > _MAX_DESCRIPTION_LENGTH:
            raise ValueError("capability description must be 1-200 characters")
        object.__setattr__(self, "description", description)
        if not issubclass(self.input_model, StrictCapabilityModel):
            raise TypeError("capability input model must be strict")
        if not issubclass(self.output_model, StrictCapabilityModel):
            raise TypeError("capability output model must be strict")


@dataclass(frozen=True)
class CapabilitySpecification:
    name: str
    description: str
    input_schema: Mapping[str, Any]


class DuplicateCapabilityName(ValueError):
    pass


class CapabilityRegistry:
    """Immutable registry built only from explicit MBB code definitions."""

    def __init__(self, definitions: Iterable[CapabilityDefinition]) -> None:
        registered: dict[str, CapabilityDefinition] = {}
        for definition in definitions:
            if definition.name in registered:
                raise DuplicateCapabilityName(definition.name)
            registered[definition.name] = definition
        self._definitions = MappingProxyType(registered)

    def __len__(self) -> int:
        return len(self._definitions)

    def resolve(self, name: str) -> CapabilityDefinition | None:
        return self._definitions.get(name)

    def specifications(
        self,
        allowed_capabilities: Iterable[str],
    ) -> tuple[CapabilitySpecification, ...]:
        allowed = frozenset(allowed_capabilities)
        return tuple(
            CapabilitySpecification(
                name=definition.name,
                description=definition.description,
                input_schema=definition.input_model.model_json_schema(),
            )
            for name, definition in sorted(self._definitions.items())
            if name in allowed
        )


class CapabilityErrorCategory(str, Enum):
    unknown_tool = "unknown_tool"
    tool_not_allowed = "tool_not_allowed"
    invalid_arguments = "invalid_arguments"
    execution_failed = "execution_failed"


class SafeCapabilityError(Exception):
    """Code-controlled domain failure safe to classify across the AI boundary."""

    def __init__(self, safe_code: str) -> None:
        if not _SAFE_CODE_PATTERN.fullmatch(safe_code):
            raise ValueError("safe capability error code is invalid")
        super().__init__("capability execution failed safely")
        self.safe_code = safe_code


@dataclass(frozen=True)
class CapabilitySuccess:
    capability_name: str
    output: StrictCapabilityModel
    succeeded: bool = True


@dataclass(frozen=True)
class CapabilityFailure:
    error: CapabilityErrorCategory
    safe_code: str | None = None
    succeeded: bool = False


CapabilityExecutionResult = CapabilitySuccess | CapabilityFailure


class CapabilityExecutor:
    def __init__(self, registry: CapabilityRegistry) -> None:
        self._registry = registry

    async def execute(
        self,
        *,
        requested_name: str,
        model_arguments: object,
        allowed_capabilities: Iterable[str],
        context: TrustedCapabilityContext,
    ) -> CapabilityExecutionResult:
        if not isinstance(requested_name, str):
            return CapabilityFailure(CapabilityErrorCategory.unknown_tool)
        definition = self._registry.resolve(requested_name)
        if definition is None:
            return CapabilityFailure(CapabilityErrorCategory.unknown_tool)

        allowed = frozenset(allowed_capabilities)
        if requested_name not in allowed:
            return CapabilityFailure(CapabilityErrorCategory.tool_not_allowed)

        if not isinstance(model_arguments, Mapping):
            return CapabilityFailure(CapabilityErrorCategory.invalid_arguments)
        if _MODEL_FORBIDDEN_ARGUMENTS.intersection(model_arguments):
            return CapabilityFailure(CapabilityErrorCategory.invalid_arguments)

        try:
            validated_input = definition.input_model.model_validate(
                model_arguments,
                strict=True,
            )
        except ValidationError:
            return CapabilityFailure(CapabilityErrorCategory.invalid_arguments)

        try:
            raw_output = await definition.handler(context, validated_input)
            validated_output = definition.output_model.model_validate(
                raw_output,
                strict=True,
            )
        except SafeCapabilityError as exc:
            return CapabilityFailure(
                CapabilityErrorCategory.execution_failed,
                safe_code=exc.safe_code,
            )
        except Exception:
            return CapabilityFailure(CapabilityErrorCategory.execution_failed)

        return CapabilitySuccess(
            capability_name=definition.name,
            output=validated_output,
        )


class RequestHumanHandoffInput(StrictCapabilityModel):
    reason_category: Literal[
        "customer_requested_human",
        "unsupported_action",
        "policy_exception",
        "insufficient_business_evidence",
        "repeated_misunderstanding",
        "required_capability_unavailable",
    ]


class RequestHumanHandoffOutput(StrictCapabilityModel):
    state: Literal["waiting_for_human"]
    ownership_version: int = Field(gt=0)
    escalation_ticket_id: uuid.UUID
    replayed: bool


async def _request_human_handoff(
    context: TrustedCapabilityContext,
    _arguments: StrictCapabilityModel,
) -> object:
    from app.database import async_session_factory
    from app.modules.m4_conversation.ai_handoff import (
        AIHandoffConversationNotFound,
        AIHandoffUnavailable,
        StaleAIAuthority,
        request_human_handoff,
    )

    async with async_session_factory() as session:
        try:
            result = await request_human_handoff(
                session,
                conversation_id=context.conversation_id,
                expected_ownership_version=context.expected_ownership_version,
            )
        except AIHandoffConversationNotFound as exc:
            raise SafeCapabilityError("conversation_not_found") from exc
        except StaleAIAuthority as exc:
            raise SafeCapabilityError("stale_ai_authority") from exc
        except AIHandoffUnavailable as exc:
            raise SafeCapabilityError("handoff_unavailable") from exc

    return {
        "state": "waiting_for_human",
        "ownership_version": result.ownership_version,
        "escalation_ticket_id": result.escalation_ticket_id,
        "replayed": result.replayed,
    }


AI_CAPABILITY_REGISTRY = CapabilityRegistry(
    (
        CapabilityDefinition(
            name="request_human_handoff",
            description="Pause AI and request Human attention for this conversation.",
            input_model=RequestHumanHandoffInput,
            output_model=RequestHumanHandoffOutput,
            handler=_request_human_handoff,
        ),
    )
)
