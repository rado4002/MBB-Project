from __future__ import annotations

import inspect

import pytest
from pydantic import ValidationError

from app.adapters import ai_adapter_eligibility, get_ai_adapter, get_provider_turn_adapter
from app.adapters.ai.disabled_adapter import AIAdapterDisabled, DisabledAIAdapter
from app.adapters.base import ProviderTurnAdapter
from app.ai.capabilities import AI_CAPABILITY_REGISTRY
from app.ai.provider_contract import (
    MAX_PROVIDER_OUTPUT_TOKENS,
    ProviderCapability,
    ProviderContinuationState,
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
    ProviderTurnResult,
    ProviderUsage,
)


def _request(**overrides) -> ProviderTurnRequest:
    values = {
        "messages": (ProviderMessage(role="user", content="Bonjour"),),
        "system_instruction": "MBB-owned policy",
        "max_output_tokens": 512,
    }
    values.update(overrides)
    return ProviderTurnRequest(**values)


def test_provider_identity_is_strict_minimized_and_audit_bounded():
    identity = ProviderIdentity(provider="scripted", model="offline-fixture")

    assert identity.model_dump(mode="json") == {
        "provider": "scripted",
        "model": "offline-fixture",
    }
    with pytest.raises(ValidationError):
        ProviderIdentity(provider="scripted", model="x" * 101)
    with pytest.raises(ValidationError):
        ProviderIdentity(
            provider="scripted",
            model="offline-fixture",
            raw_response={"secret": True},
        )


def test_request_contract_is_strict_bounded_and_provider_neutral():
    request = _request(reasoning_profile=ProviderReasoningProfile.minimal)

    assert request.messages[0].role == "user"
    assert request.max_output_tokens == 512
    assert request.allowed_capabilities == ()
    assert request.reasoning_profile == ProviderReasoningProfile.minimal

    with pytest.raises(ValidationError):
        ProviderTurnRequest(
            messages=(ProviderMessage(role="user", content="Bonjour"),),
            system_instruction="MBB-owned policy",
            max_output_tokens=MAX_PROVIDER_OUTPUT_TOKENS + 1,
        )
    with pytest.raises(ValidationError):
        ProviderTurnRequest(
            messages=(ProviderMessage(role="user", content="Bonjour"),),
            system_instruction="MBB-owned policy",
            max_output_tokens=512,
            reasoning_profile="provider-specific-thinking-mode",
        )
    with pytest.raises(ValidationError):
        ProviderTurnRequest(
            messages=(ProviderMessage(role="user", content="Bonjour"),),
            system_instruction="MBB-owned policy",
            max_output_tokens=512,
            unexpected=True,
        )


def test_capability_projection_uses_existing_registry_specification_shape():
    specification = AI_CAPABILITY_REGISTRY.specifications({"search_products"})[0]

    capability = ProviderCapability.from_specification(specification)

    assert capability.name == "search_products"
    assert capability.description == specification.description
    assert capability.input_schema == specification.input_schema
    assert set(capability.input_schema["properties"]) == {
        "query",
        "category_code",
        "max_budget",
        "budget_currency",
        "search_mode",
        "limit",
    }

    request = _request(allowed_capabilities=(capability,))
    assert request.allowed_capabilities[0].name == "search_products"

    with pytest.raises(ValidationError, match="unique"):
        _request(allowed_capabilities=(capability, capability))


def test_result_contract_accepts_text_tools_usage_and_rejects_malformed_results():
    text_result = ProviderTurnResult(
        text="Bonjour",
        finish_reason=ProviderFinishReason.completed,
        usage=ProviderUsage(input_tokens=10, output_tokens=2),
        provider_request_id="req_safe-123",
    )
    tool_result = ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id="call_1",
                capability_name="search_products",
                arguments={"query": "air fryer"},
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )
    mixed_result = ProviderTurnResult(
        text="Je vérifie.",
        tool_calls=tool_result.tool_calls,
        finish_reason=ProviderFinishReason.tool_call,
    )

    assert text_result.text == "Bonjour"
    assert tool_result.text is None
    assert tool_result.tool_calls[0].capability_name == "search_products"
    assert mixed_result.text == "Je vérifie."

    assert ProviderTurnResult(
        finish_reason=ProviderFinishReason.error,
    ).finish_reason == ProviderFinishReason.error
    with pytest.raises(ValidationError):
        ProviderTurnResult(finish_reason=ProviderFinishReason.completed)


def test_tool_call_preserves_provider_correlation_without_authority_fields():
    tool_call = ProviderToolCall(
        call_id="provider_call_123",
        capability_name="unsupported_but_safe_name",
        arguments={"query": "air fryer", "limit": 5},
    )

    assert tool_call.call_id == "provider_call_123"
    assert tool_call.capability_name == "unsupported_but_safe_name"
    assert tool_call.arguments == {"query": "air fryer", "limit": 5}

    for forbidden in (
        "conversation_id",
        "turn_id",
        "expected_ownership_version",
        "actor",
        "authorization",
        "allowlist",
    ):
        with pytest.raises(ValidationError):
            ProviderToolCall(
                call_id="provider_call_123",
                capability_name="search_products",
                arguments={forbidden: "model-controlled"},
            )


def test_provider_errors_are_safe_and_normalized():
    error = ProviderTurnError(
        ProviderErrorCategory.authentication,
        provider_request_id="req_auth_1",
    )
    unknown = ProviderTurnError.unknown()

    assert error.safe_code == "authentication"
    assert error.provider_request_id == "req_auth_1"
    assert "sk-" not in str(error)
    assert "authentication" in str(error)
    assert unknown.safe_code == "unknown"


def test_continuation_state_is_opaque_and_excluded_from_normal_serialization():
    continuation = ProviderContinuationState(
        value={"provider_cursor": "opaque", "step": 1}
    )
    result = ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id="call_1",
                capability_name="search_products",
                arguments={},
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
        continuation_state=continuation,
    )
    next_request = _request(continuation_state=result.continuation_state)

    assert next_request.continuation_state is continuation
    assert next_request.continuation_state.value["provider_cursor"] == "opaque"
    assert "continuation_state" not in result.model_dump(mode="json")
    assert "provider_cursor" not in str(result.model_dump(mode="json"))
    assert "continuation_state" not in next_request.model_dump(mode="json")
    assert "provider_cursor" not in repr(continuation)
    assert "provider_cursor" not in repr(result)
    assert "provider_cursor" not in repr(next_request)


def test_tool_result_message_requires_only_provider_correlation_metadata():
    message = ProviderMessage(
        role="tool_result",
        tool_call_id="call_1",
        content='{"status":"ok"}',
    )

    assert message.tool_call_id == "call_1"
    assert set(message.model_fields) == {"role", "content", "tool_call_id"}
    with pytest.raises(ValidationError, match="tool-call identifier"):
        ProviderMessage(role="tool_result", content='{"status":"ok"}')
    with pytest.raises(ValidationError, match="only valid for tool results"):
        ProviderMessage(role="user", tool_call_id="call_1", content="Bonjour")


def test_tool_result_envelope_is_strict_safe_and_serializes_for_continuation():
    success = ProviderToolResult(
        call_id="call_1",
        capability_name="search_products",
        status="success",
        output={"items": []},
    )
    failure = ProviderToolResult(
        call_id="call_2",
        capability_name="search_products",
        status="error",
        error=ProviderToolError(
            category="execution_failed",
            safe_code="catalog_unavailable",
        ),
    )

    message = success.as_message()
    assert message.role == "tool_result"
    assert message.tool_call_id == "call_1"
    assert '"status":"success"' in message.content
    assert "internal exception" not in failure.model_dump_json()
    with pytest.raises(ValidationError, match="output only"):
        ProviderToolResult(
            call_id="call_3",
            capability_name="search_products",
            status="success",
            output={"items": []},
            error=ProviderToolError(category="execution_failed"),
        )


@pytest.mark.asyncio
async def test_disabled_provider_resolution_is_default_network_free_and_fails_closed():
    adapter = get_ai_adapter()
    turn_adapter = get_provider_turn_adapter()

    assert isinstance(adapter, DisabledAIAdapter)
    assert isinstance(turn_adapter, DisabledAIAdapter)
    assert isinstance(adapter, ProviderTurnAdapter)
    assert isinstance(turn_adapter, ProviderTurnAdapter)
    assert not hasattr(adapter, "_client")
    assert ai_adapter_eligibility("disabled") == "disabled"
    assert ai_adapter_eligibility("local") == "disabled"
    assert ai_adapter_eligibility("unknown-provider") == "unavailable"

    with pytest.raises(AIAdapterDisabled):
        await adapter.generate_turn(_request())


def test_provider_contract_has_no_provider_specific_terms_or_capability_execution():
    import app.ai.provider_contract as contract_module

    source = inspect.getsource(contract_module)
    for prohibited in (
        "DeepSeek",
        "OpenAI",
        "Anthropic",
        "reasoning_content",
        "CapabilityExecutor",
        "async_session",
        "requests.",
        "httpx",
    ):
        assert prohibited not in source


def test_future_provider_conformance_boundary_is_provider_neutral():
    methods = set(ProviderTurnAdapter.__abstractmethods__)

    assert methods == {"generate_turn"}
    signature = inspect.signature(ProviderTurnAdapter.generate_turn)
    assert "request" in signature.parameters
    assert signature.parameters["request"].annotation is ProviderTurnRequest
    assert signature.return_annotation is ProviderTurnResult
