from __future__ import annotations

import inspect
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

import app.adapters as adapters
from app.adapters.ai.deepseek_adapter import (
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_PROVIDER_NAME,
    DeepSeekAdapter,
    _DeepSeekHTTPTransport,
    _reasoning_settings,
)
from app.adapters.base import ProviderTurnAdapter
from app.ai.capabilities import AI_CAPABILITY_REGISTRY
from app.ai.provider_contract import (
    ProviderCapability,
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderTurnError,
    ProviderTurnRequest,
)
from app.config import Settings


class _FakeTransport:
    def __init__(
        self,
        response: Mapping[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.response = response
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def create_chat_completion(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        assert self.response is not None
        return self.response


def _capability(name: str = "search_products") -> ProviderCapability:
    specification = AI_CAPABILITY_REGISTRY.specifications({name})[0]
    return ProviderCapability.from_specification(specification)


def _request(**overrides: Any) -> ProviderTurnRequest:
    values: dict[str, Any] = {
        "messages": (ProviderMessage(role="user", content="Bonjour"),),
        "system_instruction": "MBB-owned policy",
        "max_output_tokens": 512,
    }
    values.update(overrides)
    return ProviderTurnRequest(**values)


def _response(
    *,
    content: str | None = "Bonjour, comment puis-je aider ?",
    reasoning_content: str | None = None,
    tool_calls: list[dict[str, Any]] | None = None,
    finish_reason: str = "stop",
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl_deepseek-safe-1",
        "object": "chat.completion",
        "model": DEEPSEEK_DEFAULT_MODEL,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": {
            "prompt_tokens": 17,
            "completion_tokens": 9,
            "total_tokens": 26,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 17,
        },
    }


def _tool_call(
    call_id: str = "call_search_1",
    name: str = "search_products",
    arguments: str = '{"query":"air fryer","limit":5}',
) -> dict[str, Any]:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


def test_adapter_exposes_only_safe_provider_identity_metadata() -> None:
    adapter = DeepSeekAdapter(
        api_key="fictional-key",
        model=DEEPSEEK_DEFAULT_MODEL,
        transport=_FakeTransport(_response()),
    )

    assert adapter.provider_identity is not None
    assert adapter.provider_identity.model_dump(mode="json") == {
        "provider": DEEPSEEK_PROVIDER_NAME,
        "model": DEEPSEEK_DEFAULT_MODEL,
    }
    assert "fictional-key" not in adapter.provider_identity.model_dump_json()


@pytest.mark.asyncio
async def test_http_transport_uses_fixed_endpoint_bearer_auth_and_one_offline_attempt() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(
            200,
            content=b'\n\n{"id":"safe","choices":[]}\n',
            headers={"content-type": "application/json"},
        )

    transport = _DeepSeekHTTPTransport(
        api_key="fake-key",
        timeout_s=60,
        http_transport=httpx.MockTransport(handler),
    )

    decoded = await transport.create_chat_completion({"stream": False})

    assert decoded == {"id": "safe", "choices": []}
    assert len(attempts) == 1
    assert str(attempts[0].url) == "https://api.deepseek.com/chat/completions"
    assert attempts[0].headers["authorization"] == "Bearer fake-key"
    assert attempts[0].headers["content-type"] == "application/json"


@pytest.mark.asyncio
async def test_text_completion_translates_without_reasoning_leak() -> None:
    transport = _FakeTransport(
        _response(reasoning_content="private synthetic reasoning")
    )
    adapter = DeepSeekAdapter(api_key="fake-key", transport=transport)

    result = await adapter.generate_turn(_request())

    assert isinstance(adapter, ProviderTurnAdapter)
    assert adapter.provider_name == DEEPSEEK_PROVIDER_NAME
    assert adapter.model == DEEPSEEK_DEFAULT_MODEL
    assert result.text == "Bonjour, comment puis-je aider ?"
    assert result.tool_calls == ()
    assert result.finish_reason == ProviderFinishReason.completed
    assert result.provider_request_id == "chatcmpl_deepseek-safe-1"
    assert result.usage is not None
    assert result.usage.input_tokens == 17
    assert result.usage.output_tokens == 9
    assert result.usage.total_tokens == 26
    assert result.continuation_state is None
    assert "private synthetic reasoning" not in repr(result)
    assert "private synthetic reasoning" not in result.model_dump_json()

    payload = transport.calls[0]
    assert payload["model"] == DEEPSEEK_DEFAULT_MODEL
    assert payload["messages"] == [
        {"role": "system", "content": "MBB-owned policy"},
        {"role": "user", "content": "Bonjour"},
    ]
    assert payload["max_tokens"] == 512
    assert payload["stream"] is False
    assert payload["thinking"] == {"type": "enabled"}
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "tools" not in payload
    assert "tool_choice" not in payload
    assert "user_id" not in payload


@pytest.mark.asyncio
async def test_oversized_reasoning_payload_is_rejected_without_retention() -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(reasoning_content="x" * 64_001)
        ),
    )

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == ProviderErrorCategory.malformed_response


@pytest.mark.asyncio
async def test_capability_translation_uses_only_supplied_definitions_without_strict_mode() -> None:
    transport = _FakeTransport(_response())
    adapter = DeepSeekAdapter(api_key="fake-key", transport=transport)
    capability = _capability()

    await adapter.generate_turn(_request(allowed_capabilities=(capability,)))

    payload = transport.calls[0]
    assert payload["tools"] == [
        {
            "type": "function",
            "function": {
                "name": capability.name,
                "description": capability.description,
                "parameters": capability.input_schema,
            },
        }
    ]
    assert "strict" not in payload["tools"][0]["function"]
    assert "tool_choice" not in payload

    source = inspect.getsource(DeepSeekAdapter)
    assert "AI_CAPABILITY_REGISTRY" not in source
    assert "CapabilityExecutor" not in source


@pytest.mark.asyncio
async def test_tool_calls_normalize_all_calls_without_execution() -> None:
    transport = _FakeTransport(
        _response(
            content="Je vérifie.",
            reasoning_content="private synthetic tool reasoning",
            tool_calls=[
                _tool_call(),
                _tool_call(
                    call_id="call_details_2",
                    name="get_product_details",
                    arguments='{"product_id":"fictional-product"}',
                ),
            ],
            finish_reason="tool_calls",
        )
    )
    adapter = DeepSeekAdapter(api_key="fake-key", transport=transport)

    result = await adapter.generate_turn(
        _request(allowed_capabilities=(_capability(),))
    )

    assert result.text == "Je vérifie."
    assert result.finish_reason == ProviderFinishReason.tool_call
    assert [call.call_id for call in result.tool_calls] == [
        "call_search_1",
        "call_details_2",
    ]
    assert [call.capability_name for call in result.tool_calls] == [
        "search_products",
        "get_product_details",
    ]
    assert result.tool_calls[0].arguments == {"query": "air fryer", "limit": 5}
    assert result.tool_calls[1].arguments == {"product_id": "fictional-product"}
    assert result.continuation_state is not None
    assert "private synthetic tool reasoning" not in repr(result)
    assert "private synthetic tool reasoning" not in repr(result.continuation_state)
    assert "continuation_state" not in result.model_dump(mode="json")


@pytest.mark.asyncio
async def test_thinking_tool_continuation_replays_opaque_assistant_state() -> None:
    first_transport = _FakeTransport(
        _response(
            content="Je cherche.",
            reasoning_content="private synthetic continuation",
            tool_calls=[_tool_call()],
            finish_reason="tool_calls",
        )
    )
    adapter = DeepSeekAdapter(api_key="fake-key", transport=first_transport)
    first_request = _request(allowed_capabilities=(_capability(),))
    first_result = await adapter.generate_turn(first_request)
    assert first_result.continuation_state is not None

    continuation_request = _request(
        messages=(
            ProviderMessage(role="user", content="Bonjour"),
            ProviderMessage(
                role="tool_result",
                tool_call_id="call_search_1",
                content='{"results":[{"name":"Fictional Air Fryer"}]}',
            ),
        ),
        allowed_capabilities=(_capability(),),
        continuation_state=first_result.continuation_state,
    )
    payload = adapter.build_request_payload(continuation_request)

    assert payload["messages"] == [
        {"role": "system", "content": "MBB-owned policy"},
        {"role": "user", "content": "Bonjour"},
        {
            "role": "assistant",
            "content": "Je cherche.",
            "reasoning_content": "private synthetic continuation",
            "tool_calls": [_tool_call()],
        },
        {
            "role": "tool",
            "tool_call_id": "call_search_1",
            "content": '{"results":[{"name":"Fictional Air Fryer"}]}',
        },
    ]
    assert "continuation_state" not in continuation_request.model_dump(mode="json")
    assert "private synthetic continuation" not in repr(continuation_request)


def test_tampered_continuation_state_fails_closed() -> None:
    from app.ai.provider_contract import ProviderContinuationState

    adapter = DeepSeekAdapter(api_key="fake-key", transport=_FakeTransport(_response()))
    request = _request(
        continuation_state=ProviderContinuationState(
            value={
                "provider": "deepseek",
                "model": DEEPSEEK_DEFAULT_MODEL,
                "assistant_messages": [
                    {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [_tool_call()],
                        "provider_injected_field": "forbidden",
                    }
                ],
            }
        ),
        messages=(
            ProviderMessage(
                role="tool_result",
                tool_call_id="call_search_1",
                content="fictional result",
            ),
        ),
    )

    with pytest.raises(ProviderTurnError) as captured:
        adapter.build_request_payload(request)

    assert captured.value.category == ProviderErrorCategory.invalid_request


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        (
            ProviderReasoningProfile.default,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "high"},
        ),
        (
            ProviderReasoningProfile.minimal,
            {"thinking": {"type": "disabled"}},
        ),
        (
            ProviderReasoningProfile.standard,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "low"},
        ),
        (
            ProviderReasoningProfile.strong,
            {"thinking": {"type": "enabled"}, "reasoning_effort": "max"},
        ),
    ],
)
def test_every_reasoning_profile_has_an_explicit_mapping(profile, expected) -> None:
    assert _reasoning_settings(profile) == expected
    assert "temperature" not in expected
    assert "top_p" not in expected


def test_reasoning_mapping_covers_the_complete_provider_neutral_enum() -> None:
    mapped_profiles = {
        profile
        for profile in ProviderReasoningProfile
        if _reasoning_settings(profile)
    }
    assert mapped_profiles == set(ProviderReasoningProfile)


def test_unsupported_reasoning_profile_fails_safely() -> None:
    with pytest.raises(ProviderTurnError) as captured:
        _reasoning_settings("future-provider-value")  # type: ignore[arg-type]

    assert captured.value.category == ProviderErrorCategory.invalid_request


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("native_reason", "expected"),
    [
        ("stop", ProviderFinishReason.completed),
        ("length", ProviderFinishReason.max_output),
        ("content_filter", ProviderFinishReason.stopped),
        ("future_reason", ProviderFinishReason.unknown),
    ],
)
async def test_finish_reasons_normalize_conservatively(native_reason, expected) -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(_response(finish_reason=native_reason)),
    )

    result = await adapter.generate_turn(_request())

    assert result.finish_reason == expected


@pytest.mark.asyncio
async def test_provider_resource_finish_normalizes_to_unavailable_error() -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(content=None, finish_reason="insufficient_system_resource")
        ),
    )

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == ProviderErrorCategory.unavailable
    assert captured.value.provider_request_id == "chatcmpl_deepseek-safe-1"


@pytest.mark.asyncio
async def test_non_thinking_tool_call_does_not_require_or_retain_reasoning() -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(
                content=None,
                tool_calls=[_tool_call()],
                finish_reason="tool_calls",
            )
        ),
    )

    result = await adapter.generate_turn(
        _request(reasoning_profile=ProviderReasoningProfile.minimal)
    )

    assert result.continuation_state is not None
    assistant_message = result.continuation_state.value["assistant_messages"][0]
    assert "reasoning_content" not in assistant_message


@pytest.mark.asyncio
async def test_structurally_valid_semantically_invalid_arguments_are_not_repaired() -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(
                content=None,
                reasoning_content="private",
                tool_calls=[_tool_call(arguments='{"limit":"five"}')],
                finish_reason="tool_calls",
            )
        ),
    )

    result = await adapter.generate_turn(_request())

    assert result.tool_calls[0].arguments == {"limit": "five"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_calls", "finish_reason"),
    [([_tool_call()], "stop"), (None, "tool_calls")],
)
async def test_tool_calls_and_finish_reason_must_be_consistent(
    tool_calls,
    finish_reason,
) -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(
                content="provider text",
                reasoning_content="private",
                tool_calls=tool_calls,
                finish_reason=finish_reason,
            )
        ),
    )

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == ProviderErrorCategory.malformed_response


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    ["not-json", "[]", '"text"', "null"],
)
async def test_malformed_tool_arguments_fail_as_malformed_response(arguments) -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(
                content=None,
                reasoning_content="private",
                tool_calls=[_tool_call(arguments=arguments)],
                finish_reason="tool_calls",
            )
        ),
    )

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == ProviderErrorCategory.malformed_response
    assert "not-json" not in str(captured.value)
    assert captured.value.__cause__ is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"id": "safe-id", "choices": []},
        {"id": "safe-id", "choices": [{"finish_reason": "stop"}]},
        {
            "id": "safe-id",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": None},
                }
            ],
        },
    ],
)
async def test_malformed_provider_payload_fails_safely(payload) -> None:
    adapter = DeepSeekAdapter(api_key="fake-key", transport=_FakeTransport(payload))

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == ProviderErrorCategory.malformed_response


@pytest.mark.asyncio
async def test_duplicate_provider_tool_call_ids_are_rejected() -> None:
    adapter = DeepSeekAdapter(
        api_key="fake-key",
        transport=_FakeTransport(
            _response(
                content=None,
                reasoning_content="private",
                tool_calls=[_tool_call(), _tool_call(name="get_product_details")],
                finish_reason="tool_calls",
            )
        ),
    )

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == ProviderErrorCategory.malformed_response


def _status_error(status_code: int, *, body: str = "private body") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    response = httpx.Response(
        status_code,
        request=request,
        text=body,
        headers={"x-request-id": "req_safe_error"},
    )
    return httpx.HTTPStatusError("provider status error", request=request, response=response)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("provider_error", "expected"),
    [
        (_status_error(400), ProviderErrorCategory.invalid_request),
        (_status_error(401, body="secret-key-must-not-leak"), ProviderErrorCategory.authentication),
        (_status_error(402), ProviderErrorCategory.configuration),
        (_status_error(403), ProviderErrorCategory.permission),
        (_status_error(422), ProviderErrorCategory.invalid_request),
        (_status_error(429), ProviderErrorCategory.rate_limit),
        (_status_error(500), ProviderErrorCategory.unavailable),
        (_status_error(503), ProviderErrorCategory.unavailable),
        (_status_error(418), ProviderErrorCategory.unknown),
        (
            httpx.ReadTimeout(
                "timed out",
                request=httpx.Request(
                    "POST", "https://api.deepseek.com/chat/completions"
                ),
            ),
            ProviderErrorCategory.timeout,
        ),
        (
            httpx.ConnectError(
                "unavailable",
                request=httpx.Request(
                    "POST", "https://api.deepseek.com/chat/completions"
                ),
            ),
            ProviderErrorCategory.unavailable,
        ),
        (RuntimeError("private provider detail"), ProviderErrorCategory.unknown),
    ],
)
async def test_provider_errors_are_normalized_without_raw_details(
    provider_error,
    expected,
) -> None:
    transport = _FakeTransport(error=provider_error)
    adapter = DeepSeekAdapter(api_key="fake-key", transport=transport)

    with pytest.raises(ProviderTurnError) as captured:
        await adapter.generate_turn(_request())

    assert captured.value.category == expected
    assert "private" not in str(captured.value)
    assert "secret" not in str(captured.value)
    assert captured.value.__cause__ is None
    if isinstance(provider_error, httpx.HTTPStatusError):
        assert captured.value.provider_request_id == "req_safe_error"
    assert len(transport.calls) == 1


def test_configuration_defaults_and_missing_key_fail_closed_without_transport() -> None:
    settings = Settings(
        ai_adapter="disabled",
        deepseek_api_key="",
        deepseek_model=DEEPSEEK_DEFAULT_MODEL,
        deepseek_timeout_s=60,
    )

    assert settings.ai_adapter == "disabled"
    assert settings.deepseek_model == DEEPSEEK_DEFAULT_MODEL
    assert settings.deepseek_timeout_s == 60
    assert not hasattr(settings, "deepseek_base_url")
    assert "deepseek_api_key" not in repr(settings)

    with pytest.raises(ProviderTurnError) as captured:
        DeepSeekAdapter(api_key="   ")
    assert captured.value.category == ProviderErrorCategory.configuration
    with pytest.raises(ProviderTurnError) as captured:
        DeepSeekAdapter(api_key=None)  # type: ignore[arg-type]
    assert captured.value.category == ProviderErrorCategory.configuration


def test_provider_resolution_supports_deepseek_and_missing_key_fails_closed(
    monkeypatch,
) -> None:
    monkeypatch.setattr(adapters.settings, "deepseek_api_key", "")
    with pytest.raises(ProviderTurnError) as captured:
        adapters._build_provider_turn_adapter("deepseek")
    assert captured.value.category == ProviderErrorCategory.configuration

    monkeypatch.setattr(adapters.settings, "deepseek_api_key", "fake-key")
    adapter = adapters._build_provider_turn_adapter("deepseek")
    assert isinstance(adapter, DeepSeekAdapter)
    assert adapters.ai_adapter_eligibility("deepseek") == "eligible"
    with pytest.raises(ValueError, match="Unknown AI adapter"):
        adapters._build_ai_adapter("deepseek")


def test_adapter_has_one_fixed_provider_egress_and_no_business_access() -> None:
    import app.adapters.ai.deepseek_adapter as module

    source = inspect.getsource(module)
    assert source.count("https://api.deepseek.com/chat/completions") == 1
    for prohibited in (
        "CapabilityExecutor",
        "ProductOfferService",
        "AI_CAPABILITY_REGISTRY",
        "async_session",
        "commercial_state",
        "customer_id",
        "conversation_id",
        "user_id",
        "tool_choice",
        "strict\": true",
        "temperature",
        "top_p",
        "retry",
    ):
        assert prohibited not in source
