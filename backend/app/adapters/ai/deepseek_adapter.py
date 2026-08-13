"""DeepSeek Chat Completions translation for the MBB provider-turn boundary."""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.adapters.base import ProviderTurnAdapter
from app.ai.provider_contract import (
    MAX_PROVIDER_TOOL_CALLS,
    MAX_PROVIDER_MESSAGE_CHARS,
    ProviderContinuationState,
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderUsage,
)

DEEPSEEK_PROVIDER_NAME = "deepseek"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"

_CHAT_COMPLETIONS_URL = "https://api.deepseek.com/chat/completions"
_SAFE_PROVIDER_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_MAX_CONTINUATION_ASSISTANT_MESSAGES = 16
_MAX_REASONING_CONTENT_CHARS = MAX_PROVIDER_MESSAGE_CHARS * 4


class DeepSeekChatTransport(Protocol):
    """Provider-internal transport seam used by deterministic adapter tests."""

    async def create_chat_completion(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        """Return one decoded DeepSeek Chat Completions response."""


class _DeepSeekHTTPTransport:
    def __init__(
        self,
        *,
        api_key: str,
        timeout_s: float,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._timeout = httpx.Timeout(timeout_s)
        self._http_transport = http_transport

    async def create_chat_completion(self, payload: dict[str, Any]) -> Mapping[str, Any]:
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=False,
            transport=self._http_transport,
        ) as client:
            response = await client.post(
                _CHAT_COMPLETIONS_URL,
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            response.raise_for_status()
            decoded = response.json()
        if not isinstance(decoded, Mapping):
            raise ValueError("provider response must be a JSON object")
        return decoded


class DeepSeekAdapter(ProviderTurnAdapter):
    """Translate one MBB provider turn to and from DeepSeek without tool execution."""

    provider_name = DEEPSEEK_PROVIDER_NAME

    def __init__(
        self,
        *,
        api_key: str,
        model: str = DEEPSEEK_DEFAULT_MODEL,
        timeout_s: float = 60.0,
        transport: DeepSeekChatTransport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not isinstance(model, str):
            raise ProviderTurnError(ProviderErrorCategory.configuration)
        normalized_key = api_key.strip()
        if not normalized_key:
            raise ProviderTurnError(ProviderErrorCategory.configuration)
        if not model.strip() or timeout_s <= 0:
            raise ProviderTurnError(ProviderErrorCategory.configuration)

        self.model = model.strip()
        self._transport = transport or _DeepSeekHTTPTransport(
            api_key=normalized_key,
            timeout_s=timeout_s,
        )

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        payload = self.build_request_payload(request)
        try:
            response = await self._transport.create_chat_completion(payload)
            return self.parse_response(response, request=request)
        except ProviderTurnError:
            raise
        except httpx.TimeoutException:
            raise ProviderTurnError(ProviderErrorCategory.timeout) from None
        except httpx.HTTPStatusError as exc:
            raise _normalize_http_error(exc) from None
        except httpx.RequestError:
            raise ProviderTurnError(ProviderErrorCategory.unavailable) from None
        except (TypeError, ValueError, ValidationError, json.JSONDecodeError):
            raise ProviderTurnError(ProviderErrorCategory.malformed_response) from None
        except Exception:
            raise ProviderTurnError.unknown() from None

    def build_request_payload(self, request: ProviderTurnRequest) -> dict[str, Any]:
        """Translate a validated provider-neutral request into DeepSeek JSON."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": _translate_messages(request, model=self.model),
            "max_tokens": request.max_output_tokens,
            "stream": False,
        }
        payload.update(_reasoning_settings(request.reasoning_profile))
        if request.allowed_capabilities:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": capability.name,
                        "description": capability.description,
                        "parameters": capability.input_schema,
                    },
                }
                for capability in request.allowed_capabilities
            ]
        return payload

    def parse_response(
        self,
        response: Mapping[str, Any],
        *,
        request: ProviderTurnRequest,
    ) -> ProviderTurnResult:
        """Normalize one provider-shaped response into the MBB result contract."""
        request_id = _safe_provider_identifier(response.get("id"))
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1:
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            )
        choice = choices[0]
        if not isinstance(choice, Mapping):
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            )

        native_finish_reason = choice.get("finish_reason")
        if native_finish_reason == "insufficient_system_resource":
            raise ProviderTurnError(
                ProviderErrorCategory.unavailable,
                provider_request_id=request_id,
            )
        finish_reason = _normalize_finish_reason(native_finish_reason)
        message = choice.get("message")
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            )

        raw_content = message.get("content")
        if raw_content is not None and not isinstance(raw_content, str):
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            )
        if raw_content is not None and len(raw_content) > MAX_PROVIDER_MESSAGE_CHARS:
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            )
        text = raw_content if raw_content else None
        tool_calls, provider_tool_calls = _parse_tool_calls(message.get("tool_calls"))
        if bool(tool_calls) != (native_finish_reason == "tool_calls"):
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            )

        continuation_state = None
        reasoning_enabled = (
            _reasoning_settings(request.reasoning_profile)["thinking"]["type"]
            == "enabled"
        )
        reasoning_content = message.get("reasoning_content")
        if reasoning_enabled and reasoning_content is not None:
            if not isinstance(reasoning_content, str) or not reasoning_content:
                raise ProviderTurnError(
                    ProviderErrorCategory.malformed_response,
                    provider_request_id=request_id,
                )
            if len(reasoning_content) > _MAX_REASONING_CONTENT_CHARS:
                raise ProviderTurnError(
                    ProviderErrorCategory.malformed_response,
                    provider_request_id=request_id,
                )
        if tool_calls:
            if reasoning_enabled and not isinstance(reasoning_content, str):
                raise ProviderTurnError(
                    ProviderErrorCategory.malformed_response,
                    provider_request_id=request_id,
                )
            continuation_state = _next_continuation_state(
                request.continuation_state,
                model=self.model,
                content=raw_content or "",
                reasoning_content=(reasoning_content if reasoning_enabled else None),
                tool_calls=provider_tool_calls,
            )

        try:
            return ProviderTurnResult(
                text=text,
                tool_calls=tool_calls,
                finish_reason=finish_reason,
                usage=_parse_usage(response.get("usage")),
                provider_request_id=request_id,
                continuation_state=continuation_state,
            )
        except ValidationError:
            raise ProviderTurnError(
                ProviderErrorCategory.malformed_response,
                provider_request_id=request_id,
            ) from None


def _reasoning_settings(profile: ProviderReasoningProfile) -> dict[str, Any]:
    mappings: dict[ProviderReasoningProfile, dict[str, Any]] = {
        ProviderReasoningProfile.default: {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        },
        ProviderReasoningProfile.minimal: {
            "thinking": {"type": "disabled"},
        },
        ProviderReasoningProfile.standard: {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "low",
        },
        ProviderReasoningProfile.strong: {
            "thinking": {"type": "enabled"},
            "reasoning_effort": "max",
        },
    }
    try:
        return mappings[profile]
    except KeyError:
        raise ProviderTurnError(ProviderErrorCategory.invalid_request) from None


def _translate_messages(
    request: ProviderTurnRequest,
    *,
    model: str,
) -> list[dict[str, Any]]:
    translated: list[dict[str, Any]] = [
        {"role": "system", "content": request.system_instruction}
    ]
    continuation_messages = _continuation_messages(
        request.continuation_state,
        model=model,
    )
    assistant_by_call_id: dict[str, tuple[int, dict[str, Any]]] = {}
    expected_tool_result_ids: set[str] = set()
    for index, assistant_message in enumerate(continuation_messages):
        for tool_call in assistant_message["tool_calls"]:
            call_id = tool_call["id"]
            if call_id in assistant_by_call_id:
                raise ProviderTurnError(ProviderErrorCategory.invalid_request)
            assistant_by_call_id[call_id] = (index, assistant_message)
            expected_tool_result_ids.add(call_id)

    supplied_tool_result_ids = [
        message.tool_call_id
        for message in request.messages
        if message.role == "tool_result"
    ]
    if len(supplied_tool_result_ids) != len(set(supplied_tool_result_ids)):
        raise ProviderTurnError(ProviderErrorCategory.invalid_request)
    if expected_tool_result_ids != set(supplied_tool_result_ids):
        raise ProviderTurnError(ProviderErrorCategory.invalid_request)

    emitted_assistant_indexes: set[int] = set()
    for message in request.messages:
        if message.role == "tool_result":
            correlation = assistant_by_call_id.get(message.tool_call_id or "")
            if correlation is None:
                raise ProviderTurnError(ProviderErrorCategory.invalid_request)
            assistant_index, assistant_message = correlation
            if assistant_index not in emitted_assistant_indexes:
                translated.append(assistant_message)
                emitted_assistant_indexes.add(assistant_index)
            translated.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message.content,
                }
            )
        else:
            translated.append({"role": message.role, "content": message.content})

    if len(emitted_assistant_indexes) != len(continuation_messages):
        raise ProviderTurnError(ProviderErrorCategory.invalid_request)
    return translated


def _continuation_messages(
    continuation_state: ProviderContinuationState | None,
    *,
    model: str,
) -> list[dict[str, Any]]:
    if continuation_state is None:
        return []
    value = continuation_state.value
    if set(value) != {"provider", "model", "assistant_messages"}:
        raise ProviderTurnError(ProviderErrorCategory.invalid_request)
    if value.get("provider") != DEEPSEEK_PROVIDER_NAME or value.get("model") != model:
        raise ProviderTurnError(ProviderErrorCategory.invalid_request)
    messages = value.get("assistant_messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= _MAX_CONTINUATION_ASSISTANT_MESSAGES:
        raise ProviderTurnError(ProviderErrorCategory.invalid_request)
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        if not set(message).issubset(
            {"role", "content", "reasoning_content", "tool_calls"}
        ):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        if not isinstance(message.get("content"), str):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        if len(message["content"]) > MAX_PROVIDER_MESSAGE_CHARS:
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        tool_calls = message.get("tool_calls")
        if (
            not isinstance(tool_calls, list)
            or not 1 <= len(tool_calls) <= MAX_PROVIDER_TOOL_CALLS
        ):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        _validate_continuation_tool_calls(tool_calls)
        reasoning_content = message.get("reasoning_content")
        if reasoning_content is not None and not isinstance(reasoning_content, str):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        if (
            isinstance(reasoning_content, str)
            and len(reasoning_content) > _MAX_REASONING_CONTENT_CHARS
        ):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
    return messages


def _next_continuation_state(
    previous: ProviderContinuationState | None,
    *,
    model: str,
    content: str,
    reasoning_content: str | None,
    tool_calls: list[dict[str, Any]],
) -> ProviderContinuationState:
    assistant_messages = list(_continuation_messages(previous, model=model))
    assistant_message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls,
    }
    if reasoning_content is not None:
        assistant_message["reasoning_content"] = reasoning_content
    assistant_messages.append(assistant_message)
    if len(assistant_messages) > _MAX_CONTINUATION_ASSISTANT_MESSAGES:
        raise ProviderTurnError(ProviderErrorCategory.malformed_response)
    return ProviderContinuationState(
        value={
            "provider": DEEPSEEK_PROVIDER_NAME,
            "model": model,
            "assistant_messages": assistant_messages,
        }
    )


def _parse_tool_calls(
    raw_tool_calls: Any,
) -> tuple[tuple[ProviderToolCall, ...], list[dict[str, Any]]]:
    if raw_tool_calls is None:
        return (), []
    if not isinstance(raw_tool_calls, list) or not 1 <= len(raw_tool_calls) <= MAX_PROVIDER_TOOL_CALLS:
        raise ProviderTurnError(ProviderErrorCategory.malformed_response)

    normalized: list[ProviderToolCall] = []
    provider_calls: list[dict[str, Any]] = []
    seen_call_ids: set[str] = set()
    for raw_call in raw_tool_calls:
        if not isinstance(raw_call, Mapping) or raw_call.get("type") != "function":
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        function = raw_call.get("function")
        if not isinstance(function, Mapping):
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        call_id = raw_call.get("id")
        capability_name = function.get("name")
        raw_arguments = function.get("arguments")
        if not all(isinstance(value, str) for value in (call_id, capability_name, raw_arguments)):
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        if len(raw_arguments) > _MAX_REASONING_CONTENT_CHARS:
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        if call_id in seen_call_ids:
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        seen_call_ids.add(call_id)
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            raise ProviderTurnError(ProviderErrorCategory.malformed_response) from None
        if not isinstance(arguments, dict):
            raise ProviderTurnError(ProviderErrorCategory.malformed_response)
        try:
            normalized.append(
                ProviderToolCall(
                    call_id=call_id,
                    capability_name=capability_name,
                    arguments=arguments,
                )
            )
        except ValidationError:
            raise ProviderTurnError(ProviderErrorCategory.malformed_response) from None
        provider_calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": capability_name,
                    "arguments": raw_arguments,
                },
            }
        )
    return tuple(normalized), provider_calls


def _validate_continuation_tool_calls(tool_calls: list[Any]) -> None:
    seen_call_ids: set[str] = set()
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict) or set(tool_call) != {"id", "type", "function"}:
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        function = tool_call.get("function")
        if (
            tool_call.get("type") != "function"
            or not isinstance(function, dict)
            or set(function) != {"name", "arguments"}
        ):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        call_id = tool_call.get("id")
        name = function.get("name")
        arguments = function.get("arguments")
        if not all(isinstance(value, str) for value in (call_id, name, arguments)):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        if call_id in seen_call_ids or len(arguments) > _MAX_REASONING_CONTENT_CHARS:
            raise ProviderTurnError(ProviderErrorCategory.invalid_request)
        seen_call_ids.add(call_id)
        try:
            ProviderToolCall(
                call_id=call_id,
                capability_name=name,
                arguments=json.loads(arguments),
            )
        except (json.JSONDecodeError, TypeError, ValidationError):
            raise ProviderTurnError(ProviderErrorCategory.invalid_request) from None


def _parse_usage(raw_usage: Any) -> ProviderUsage | None:
    if raw_usage is None:
        return None
    if not isinstance(raw_usage, Mapping):
        raise ProviderTurnError(ProviderErrorCategory.malformed_response)
    try:
        return ProviderUsage(
            input_tokens=raw_usage.get("prompt_tokens"),
            output_tokens=raw_usage.get("completion_tokens"),
            total_tokens=raw_usage.get("total_tokens"),
        )
    except ValidationError:
        raise ProviderTurnError(ProviderErrorCategory.malformed_response) from None


def _normalize_finish_reason(native_reason: Any) -> ProviderFinishReason:
    if not isinstance(native_reason, str):
        raise ProviderTurnError(ProviderErrorCategory.malformed_response)
    return {
        "stop": ProviderFinishReason.completed,
        "length": ProviderFinishReason.max_output,
        "content_filter": ProviderFinishReason.stopped,
        "tool_calls": ProviderFinishReason.tool_call,
        "insufficient_system_resource": ProviderFinishReason.error,
    }.get(native_reason, ProviderFinishReason.unknown)


def _normalize_http_error(error: httpx.HTTPStatusError) -> ProviderTurnError:
    status = error.response.status_code
    request_id = _safe_provider_identifier(error.response.headers.get("x-request-id"))
    category = {
        400: ProviderErrorCategory.invalid_request,
        401: ProviderErrorCategory.authentication,
        402: ProviderErrorCategory.configuration,
        403: ProviderErrorCategory.permission,
        422: ProviderErrorCategory.invalid_request,
        429: ProviderErrorCategory.rate_limit,
        500: ProviderErrorCategory.unavailable,
        502: ProviderErrorCategory.unavailable,
        503: ProviderErrorCategory.unavailable,
        504: ProviderErrorCategory.unavailable,
    }.get(status, ProviderErrorCategory.unknown)
    return ProviderTurnError(category, provider_request_id=request_id)


def _safe_provider_identifier(value: Any) -> str | None:
    if isinstance(value, str) and _SAFE_PROVIDER_IDENTIFIER.fullmatch(value):
        return value
    return None
