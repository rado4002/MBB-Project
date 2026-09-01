"""Provider-neutral AI turn contracts for model adapter boundaries."""
from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    model_validator,
)

MAX_PROVIDER_MESSAGES = 32
MAX_PROVIDER_MESSAGE_CHARS = 16_000
MAX_PROVIDER_SYSTEM_CHARS = 12_000
MAX_PROVIDER_CAPABILITIES = 16
MAX_PROVIDER_TOOL_CALLS = 16
MAX_PROVIDER_OUTPUT_TOKENS = 4096
MAX_PROVIDER_CONTINUATION_FIELDS = 16

_SAFE_NAME_PATTERN = r"^[a-z][a-z0-9_]{0,63}$"
_SAFE_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$"
_MODEL_FORBIDDEN_ARGUMENTS = frozenset(
    {
        "allowed_tools",
        "allowlist",
        "actor",
        "actor_id",
        "authorization",
        "business_id",
        "conversation_id",
        "customer_id",
        "expected_ownership_version",
        "human_owner_account_id",
        "internal_account_id",
        "owner_id",
        "owner_type",
        "ownership_version",
        "permissions",
        "tenant_id",
        "turn_id",
    }
)

SafeProviderName = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=64,
        pattern=_SAFE_NAME_PATTERN,
    ),
]
SafeProviderIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=128,
        pattern=_SAFE_IDENTIFIER_PATTERN,
    ),
]
SafeProviderMetadataIdentifier = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        min_length=1,
        max_length=100,
        pattern=_SAFE_IDENTIFIER_PATTERN,
    ),
]


class StrictProviderModel(BaseModel):
    """Strict base model for provider-neutral adapter contracts."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        protected_namespaces=(),
        strict=True,
    )


class ProviderIdentity(StrictProviderModel):
    """Safe configured provider/model identity for provider-neutral provenance."""

    provider: SafeProviderMetadataIdentifier
    model: SafeProviderMetadataIdentifier | None = None


class ProviderReasoningProfile(str, Enum):
    """MBB-owned intent for provider-side reasoning effort."""

    default = "default"
    minimal = "minimal"
    standard = "standard"
    strong = "strong"


class ProviderFinishReason(str, Enum):
    """Small normalized finish reason set owned by MBB."""

    completed = "completed"
    tool_call = "tool_call"
    max_output = "max_output"
    stopped = "stopped"
    error = "error"
    unknown = "unknown"


class ProviderErrorCategory(str, Enum):
    """Provider-neutral safe error categories."""

    configuration = "configuration"
    authentication = "authentication"
    permission = "permission"
    rate_limit = "rate_limit"
    timeout = "timeout"
    unavailable = "unavailable"
    invalid_request = "invalid_request"
    malformed_response = "malformed_response"
    unknown = "unknown"


class ProviderTurnError(RuntimeError):
    """Safe provider-neutral exception that never includes raw provider payloads."""

    def __init__(
        self,
        category: ProviderErrorCategory,
        *,
        provider_request_id: str | None = None,
    ) -> None:
        self.category = category
        self.safe_code = category.value
        self.provider_request_id = provider_request_id
        super().__init__(f"provider_turn_error:{self.safe_code}")

    @classmethod
    def unknown(cls) -> ProviderTurnError:
        return cls(ProviderErrorCategory.unknown)


class ProviderMessage(StrictProviderModel):
    """Provider-neutral conversation message."""

    role: Literal["user", "assistant", "tool_result"]
    content: str = Field(min_length=1, max_length=MAX_PROVIDER_MESSAGE_CHARS)
    tool_call_id: SafeProviderIdentifier | None = None

    @model_validator(mode="after")
    def tool_result_has_provider_correlation(self) -> ProviderMessage:
        if self.role == "tool_result" and self.tool_call_id is None:
            raise ValueError("tool-result messages require a tool-call identifier")
        if self.role != "tool_result" and self.tool_call_id is not None:
            raise ValueError("tool-call identifiers are only valid for tool results")
        return self


class ProviderCapability(StrictProviderModel):
    """Adapter-facing projection of an MBB capability specification."""

    name: SafeProviderName
    description: str = Field(min_length=1, max_length=200)
    input_schema: dict[str, JsonValue]

    @classmethod
    def from_specification(cls, specification: object) -> ProviderCapability:
        """Project the existing MBB capability specification into this boundary."""
        return cls.model_validate(
            {
                "name": getattr(specification, "name"),
                "description": getattr(specification, "description"),
                "input_schema": getattr(specification, "input_schema"),
            }
        )


class ProviderContinuationState(StrictProviderModel):
    """Opaque provider mechanics carried only between adapter calls."""

    value: dict[str, JsonValue] = Field(
        min_length=1,
        max_length=MAX_PROVIDER_CONTINUATION_FIELDS,
        exclude=True,
        repr=False,
    )


class ProviderTurnRequest(StrictProviderModel):
    """Provider-neutral request consumed by one model adapter call."""

    messages: tuple[ProviderMessage, ...] = Field(
        min_length=1,
        max_length=MAX_PROVIDER_MESSAGES,
    )
    system_instruction: str = Field(min_length=1, max_length=MAX_PROVIDER_SYSTEM_CHARS)
    allowed_capabilities: tuple[ProviderCapability, ...] = Field(
        default=(),
        max_length=MAX_PROVIDER_CAPABILITIES,
    )
    max_output_tokens: int = Field(ge=1, le=MAX_PROVIDER_OUTPUT_TOKENS)
    reasoning_profile: ProviderReasoningProfile = ProviderReasoningProfile.default
    continuation_state: ProviderContinuationState | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )

    @model_validator(mode="after")
    def capability_names_are_unique(self) -> ProviderTurnRequest:
        names = [capability.name for capability in self.allowed_capabilities]
        if len(names) != len(set(names)):
            raise ValueError("allowed capability names must be unique")
        return self


class ProviderToolCall(StrictProviderModel):
    """Provider-neutral model request to call one MBB capability."""

    call_id: SafeProviderIdentifier
    capability_name: SafeProviderName
    arguments: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def reject_model_supplied_authority(self) -> ProviderToolCall:
        if _MODEL_FORBIDDEN_ARGUMENTS.intersection(self.arguments):
            raise ValueError("tool-call arguments cannot include trusted MBB context")
        return self


class ProviderToolError(StrictProviderModel):
    """Safe provider-neutral capability failure details."""

    category: SafeProviderName
    safe_code: SafeProviderName | None = None


class ProviderToolResult(StrictProviderModel):
    """Safe result returned to a provider for one requested capability."""

    call_id: SafeProviderIdentifier
    capability_name: SafeProviderName
    status: Literal["success", "error"]
    output: dict[str, JsonValue] | None = None
    error: ProviderToolError | None = None

    @model_validator(mode="after")
    def status_matches_payload(self) -> ProviderToolResult:
        if self.status == "success" and (self.output is None or self.error is not None):
            raise ValueError("successful tool results require output only")
        if self.status == "error" and (self.error is None or self.output is not None):
            raise ValueError("failed tool results require error only")
        return self

    def as_message(self) -> ProviderMessage:
        """Serialize only the validated safe envelope for provider continuation."""
        return ProviderMessage(
            role="tool_result",
            tool_call_id=self.call_id,
            content=self.model_dump_json(),
        )


class ProviderUsage(StrictProviderModel):
    """Minimal token usage metadata when a provider supplies it."""

    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    cache_hit_tokens: int | None = Field(default=None, ge=0)
    cache_miss_tokens: int | None = Field(default=None, ge=0)
    reasoning_tokens: int | None = Field(default=None, ge=0)


class ProviderTurnResult(StrictProviderModel):
    """Provider-neutral result from one model adapter call."""

    text: str | None = Field(default=None, min_length=1, max_length=MAX_PROVIDER_MESSAGE_CHARS)
    tool_calls: tuple[ProviderToolCall, ...] = Field(
        default=(),
        max_length=MAX_PROVIDER_TOOL_CALLS,
    )
    finish_reason: ProviderFinishReason
    usage: ProviderUsage | None = None
    provider_request_id: SafeProviderIdentifier | None = None
    continuation_state: ProviderContinuationState | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )

    @model_validator(mode="after")
    def result_has_model_output_or_error(self) -> ProviderTurnResult:
        if self.text is None and not self.tool_calls and self.finish_reason != ProviderFinishReason.error:
            raise ValueError("provider result must contain text, tool calls, or error")
        return self
