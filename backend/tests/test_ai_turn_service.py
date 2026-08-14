import inspect
import json
import uuid

import pytest

from app.ai.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    StrictCapabilityModel,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION, get_system_policy
from app.ai.provider_contract import (
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderMessage,
    ProviderToolCall,
    ProviderTurnError,
    ProviderTurnResult,
)
from app.ai.turn import (
    AITurn,
    AITurnBudgetExceeded,
    AITurnLimits,
    AITurnService,
    StaleAITurnAuthority,
)

CONVERSATION_ID = uuid.UUID("10000000-0000-4000-8000-000000000001")


class _EchoInput(StrictCapabilityModel):
    value: str


class _EchoOutput(StrictCapabilityModel):
    value: str
    trusted_conversation_id: uuid.UUID


def _echo_registry(handler) -> CapabilityRegistry:
    return CapabilityRegistry(
        (
            CapabilityDefinition(
                name="echo_value",
                description="Return a validated test value.",
                input_model=_EchoInput,
                output_model=_EchoOutput,
                handler=handler,
            ),
        )
    )


class _SequenceAdapter:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        return self.results.pop(0)


async def _authority_allowed(_context):
    return True


def _tool_result(*calls) -> ProviderTurnResult:
    return ProviderTurnResult(
        tool_calls=tuple(calls),
        finish_reason=ProviderFinishReason.tool_call,
    )


def _tool_call(
    call_id: str = "call_1",
    *,
    name: str = "echo_value",
    arguments=None,
) -> ProviderToolCall:
    return ProviderToolCall(
        call_id=call_id,
        capability_name=name,
        arguments={"value": "validated"} if arguments is None else arguments,
    )


def _turn(**overrides) -> AITurn:
    values = {
        "user_content": "Bonjour",
        "language": "french",
        "expected_ownership_version": 7,
        "conversation_id": CONVERSATION_ID,
    }
    values.update(overrides)
    return AITurn(**values)


class _RecordingAdapter:
    def __init__(self, *, result="assistant response", error=None):
        self.result = result
        self.error = error
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        return ProviderTurnResult(
            text=self.result,
            finish_reason=ProviderFinishReason.completed,
        )

    async def detect_language(self, _text):
        return "french"


@pytest.mark.asyncio
async def test_turn_separates_policy_from_customer_runtime_content():
    customer_text = "customer-controlled-policy-injection"
    history_text = "customer-controlled-history"
    adapter = _RecordingAdapter()
    service = AITurnService(adapter)

    result = await service.generate(
        AITurn(
            user_content=customer_text,
            language="lingala",
            expected_ownership_version=7,
            conversation_id=CONVERSATION_ID,
            history=(
                {"direction": "inbound", "content": history_text},
                {"direction": "outbound", "content": "prior assistant reply"},
            ),
        )
    )

    assert result == "assistant response"
    assert len(adapter.calls) == 1
    request = adapter.calls[0]
    serialized = request.model_dump(mode="json")
    assert "turn_id" not in serialized
    assert "expected_ownership_version" not in serialized
    assert request.system_instruction == get_system_policy("lingala").text
    assert customer_text not in request.system_instruction
    assert history_text not in request.system_instruction
    assert customer_text in request.messages[0].content
    assert history_text in request.messages[0].content
    assert request.max_output_tokens == 512
    assert request.allowed_capabilities == ()


@pytest.mark.asyncio
async def test_turn_without_history_preserves_existing_user_prompt_shape():
    adapter = _RecordingAdapter()
    service = AITurnService(adapter)

    await service.generate(
        AITurn(
            user_content="Mbote",
            language="french",
            expected_ownership_version=1,
            conversation_id=CONVERSATION_ID,
        )
    )

    assert adapter.calls[0].messages == (
        ProviderMessage(role="user", content="Mbote"),
    )


@pytest.mark.asyncio
async def test_turn_preserves_existing_six_message_history_window():
    adapter = _RecordingAdapter()
    service = AITurnService(adapter)
    history = tuple(
        {"direction": "inbound", "content": f"history-{index}"}
        for index in range(8)
    )

    await service.generate(
        AITurn(
            user_content="current message",
            language="french",
            expected_ownership_version=3,
            conversation_id=CONVERSATION_ID,
            history=history,
        )
    )

    prompt = adapter.calls[0].messages[0].content
    assert "history-0" not in prompt
    assert "history-1" not in prompt
    for index in range(2, 8):
        assert f"history-{index}" in prompt
    assert "current message" in prompt


@pytest.mark.asyncio
async def test_adapter_failure_propagates_for_m1_safe_fallback():
    failure = RuntimeError("adapter unavailable")
    service = AITurnService(_RecordingAdapter(error=failure))

    with pytest.raises(RuntimeError, match="adapter unavailable"):
        await service.generate(
            AITurn(
                user_content="Mbote",
                language="french",
                expected_ownership_version=2,
                conversation_id=CONVERSATION_ID,
            )
        )


@pytest.mark.asyncio
async def test_disabled_adapter_preserves_safe_failure_without_network_client():
    from app.adapters.ai.disabled_adapter import AIAdapterDisabled, DisabledAIAdapter

    service = AITurnService(DisabledAIAdapter())

    with pytest.raises(AIAdapterDisabled, match="AI adapter disabled"):
        await service.generate(
            AITurn(
                user_content="Mbote",
                language="french",
                expected_ownership_version=2,
                conversation_id=CONVERSATION_ID,
            )
        )


@pytest.mark.asyncio
async def test_exposed_capability_executes_and_returns_safe_continuation_result():
    observed = []

    async def handler(context, arguments):
        observed.append((context, arguments))
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    registry = _echo_registry(handler)
    adapter = _SequenceAdapter(
        _tool_result(_tool_call()),
        ProviderTurnResult(
            text="Authoritative result used.",
            finish_reason=ProviderFinishReason.completed,
        ),
    )
    service = AITurnService(
        adapter,
        capability_registry=registry,
        authority_checker=_authority_allowed,
    )

    result = await service.generate(
        _turn(allowed_capabilities=("echo_value",))
    )

    assert result == "Authoritative result used."
    assert len(adapter.calls) == 2
    assert [item.name for item in adapter.calls[0].allowed_capabilities] == [
        "echo_value"
    ]
    assert len(observed) == 1
    context, arguments = observed[0]
    assert context.conversation_id == CONVERSATION_ID
    assert context.expected_ownership_version == 7
    assert arguments.value == "validated"
    continued = json.loads(adapter.calls[1].messages[-1].content)
    assert continued == {
        "call_id": "call_1",
        "capability_name": "echo_value",
        "status": "success",
        "output": {
            "value": "validated",
            "trusted_conversation_id": str(CONVERSATION_ID),
        },
        "error": None,
    }
    serialized = adapter.calls[0].model_dump(mode="json")
    assert "conversation_id" not in serialized
    assert "expected_ownership_version" not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested_name", "exposed", "expected_category"),
    [
        ("unknown_capability", ("echo_value",), "unknown_tool"),
        ("echo_value", (), "tool_not_allowed"),
    ],
)
async def test_unknown_or_unexposed_capability_never_executes(
    requested_name,
    exposed,
    expected_category,
):
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    adapter = _SequenceAdapter(
        _tool_result(_tool_call(name=requested_name)),
        ProviderTurnResult(text="Fallback", finish_reason=ProviderFinishReason.completed),
    )
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
    )

    assert await service.generate(_turn(allowed_capabilities=exposed)) == "Fallback"
    assert executions == 0
    returned = json.loads(adapter.calls[1].messages[-1].content)
    assert returned["status"] == "error"
    assert returned["error"] == {
        "category": expected_category,
        "safe_code": None,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool_call",
    [
        _tool_call(arguments={"value": 3}),
        _tool_call(arguments={"value": "valid", "unexpected": "field"}),
    ],
)
async def test_malformed_unknown_or_trusted_arguments_never_execute(tool_call):
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    adapter = _SequenceAdapter(
        _tool_result(tool_call),
        ProviderTurnResult(text="Clarify", finish_reason=ProviderFinishReason.completed),
    )
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
    )

    assert await service.generate(
        _turn(allowed_capabilities=("echo_value",))
    ) == "Clarify"
    assert executions == 0
    returned = json.loads(adapter.calls[1].messages[-1].content)
    assert returned["error"]["category"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_defense_in_depth_rejects_provider_attempt_to_override_trusted_context():
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    unchecked_call = ProviderToolCall.model_construct(
        call_id="call_1",
        capability_name="echo_value",
        arguments={"value": "valid", "conversation_id": "model-controlled"},
    )
    unchecked_result = ProviderTurnResult.model_construct(
        text=None,
        tool_calls=(unchecked_call,),
        finish_reason=ProviderFinishReason.tool_call,
        usage=None,
        provider_request_id=None,
        continuation_state=None,
    )
    adapter = _SequenceAdapter(
        unchecked_result,
        ProviderTurnResult(text="Clarify", finish_reason=ProviderFinishReason.completed),
    )
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
    )

    assert await service.generate(
        _turn(allowed_capabilities=("echo_value",))
    ) == "Clarify"
    assert executions == 0
    returned = json.loads(adapter.calls[1].messages[-1].content)
    assert returned["error"]["category"] == "invalid_arguments"


@pytest.mark.asyncio
async def test_authority_lost_after_inference_prevents_capability_execution():
    checks = iter((True, False))
    executions = 0

    async def checker(_context):
        return next(checks)

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    service = AITurnService(
        _SequenceAdapter(_tool_result(_tool_call())),
        capability_registry=_echo_registry(handler),
        authority_checker=checker,
    )

    with pytest.raises(StaleAITurnAuthority):
        await service.generate(_turn(allowed_capabilities=("echo_value",)))
    assert executions == 0


@pytest.mark.asyncio
async def test_human_takeover_during_round_stops_further_capability_execution():
    authority = True
    executions = []

    async def checker(_context):
        return authority

    async def handler(context, arguments):
        nonlocal authority
        executions.append(arguments.value)
        authority = False
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    service = AITurnService(
        _SequenceAdapter(
            _tool_result(
                _tool_call("call_1", arguments={"value": "first"}),
                _tool_call("call_2", arguments={"value": "second"}),
            )
        ),
        capability_registry=_echo_registry(handler),
        authority_checker=checker,
    )

    with pytest.raises(StaleAITurnAuthority):
        await service.generate(_turn(allowed_capabilities=("echo_value",)))
    assert executions == ["first"]


@pytest.mark.asyncio
async def test_capability_execution_budget_rejects_a_large_round_before_execution():
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    calls = tuple(_tool_call(f"call_{index}") for index in range(4))
    adapter = _SequenceAdapter(_tool_result(*calls))
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
    )

    with pytest.raises(AITurnBudgetExceeded) as captured:
        await service.generate(_turn(allowed_capabilities=("echo_value",)))
    assert captured.value.budget == "capability_executions"
    assert executions == 0
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_tool_round_budget_is_independent_and_bounded():
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    adapter = _SequenceAdapter(
        _tool_result(_tool_call("call_1")),
        _tool_result(_tool_call("call_2")),
    )
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
        limits=AITurnLimits(tool_rounds=1),
    )

    with pytest.raises(AITurnBudgetExceeded) as captured:
        await service.generate(_turn(allowed_capabilities=("echo_value",)))
    assert captured.value.budget == "tool_rounds"
    assert executions == 1
    assert len(adapter.calls) == 2


@pytest.mark.asyncio
async def test_provider_call_budget_is_independent_and_bounded():
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    adapter = _SequenceAdapter(_tool_result(_tool_call()))
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
        limits=AITurnLimits(provider_calls=1),
    )

    with pytest.raises(AITurnBudgetExceeded) as captured:
        await service.generate(_turn(allowed_capabilities=("echo_value",)))
    assert captured.value.budget == "provider_calls"
    assert executions == 1
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_capability_exception_returns_only_safe_normalized_error():
    async def handler(_context, _arguments):
        raise RuntimeError("database password and internal stack")

    adapter = _SequenceAdapter(
        _tool_result(_tool_call()),
        ProviderTurnResult(text="Safe fallback", finish_reason=ProviderFinishReason.completed),
    )
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
    )

    assert await service.generate(
        _turn(allowed_capabilities=("echo_value",))
    ) == "Safe fallback"
    returned_content = adapter.calls[1].messages[-1].content
    assert "password" not in returned_content
    assert "stack" not in returned_content
    assert json.loads(returned_content)["error"] == {
        "category": "execution_failed",
        "safe_code": None,
    }


@pytest.mark.asyncio
async def test_repeated_call_identity_fails_without_execution_or_continuation():
    executions = 0

    async def handler(context, arguments):
        nonlocal executions
        executions += 1
        return {
            "value": arguments.value,
            "trusted_conversation_id": context.conversation_id,
        }

    adapter = _SequenceAdapter(
        _tool_result(_tool_call("duplicate"), _tool_call("duplicate"))
    )
    service = AITurnService(
        adapter,
        capability_registry=_echo_registry(handler),
        authority_checker=_authority_allowed,
    )

    with pytest.raises(ProviderTurnError) as captured:
        await service.generate(_turn(allowed_capabilities=("echo_value",)))
    assert captured.value.category == ProviderErrorCategory.malformed_response
    assert executions == 0
    assert len(adapter.calls) == 1


@pytest.mark.asyncio
async def test_final_provider_text_is_rejected_after_authority_becomes_stale():
    checks = iter((True, False))

    async def checker(_context):
        return next(checks)

    service = AITurnService(
        _SequenceAdapter(
            ProviderTurnResult(
                text="Stale generated reply",
                finish_reason=ProviderFinishReason.completed,
            )
        ),
        authority_checker=checker,
    )

    with pytest.raises(StaleAITurnAuthority):
        await service.generate(_turn())


def test_limits_cannot_exceed_permanent_hard_ceilings():
    for values in (
        {"provider_calls": 4},
        {"tool_rounds": 3},
        {"capability_executions": 4},
    ):
        with pytest.raises(ValueError, match="hard ceilings"):
            AITurnLimits(**values)


def test_turn_requires_a_positive_ownership_generation():
    with pytest.raises(ValueError, match="ownership version"):
        AITurn(
            user_content="Mbote",
            language="french",
            expected_ownership_version=0,
            conversation_id=CONVERSATION_ID,
        )


def test_policy_is_explicitly_versioned_and_contains_authority_limits():
    policy = get_system_policy("french")

    assert AI_SYSTEM_POLICY_VERSION == "mbb-ai-policy-v1"
    assert policy.version == AI_SYSTEM_POLICY_VERSION
    assert "MBB AI Assistant" in policy.text
    for prohibited_fact in (
        "prices",
        "stock",
        "promotions",
        "orders",
        "payments",
        "delivery commitments",
        "permissions",
        "completed business actions",
    ):
        assert prohibited_fact in policy.text
    assert "Human operators remain authoritative" in policy.text


def test_new_ai_modules_are_provider_neutral():
    import app.ai.policy as policy_module
    import app.ai.turn as turn_module

    source = inspect.getsource(policy_module) + inspect.getsource(turn_module)
    for provider_term in ("DeepSeek", "OpenAI", "Anthropic", "reasoning_content"):
        assert provider_term not in source


def test_return_eligibility_uses_the_local_adapter_factory_without_network():
    from app.adapters import ai_adapter_eligibility

    assert ai_adapter_eligibility("disabled") == "disabled"
    assert ai_adapter_eligibility("local") == "disabled"
    assert ai_adapter_eligibility("unknown-provider") == "unavailable"
    assert ai_adapter_eligibility("claude") == "eligible"


def test_ownership_business_service_has_no_provider_literal():
    import app.modules.m4_conversation.ownership as ownership_module

    source = inspect.getsource(ownership_module)
    assert 'ai_adapter != "claude"' not in source
    assert "get_ai_adapter" not in source


def test_m1_business_seam_uses_turn_service_not_adapter_directly():
    from app.tasks import m1

    source = inspect.getsource(m1._process)
    assert "get_ai_turn_service()" in source
    assert "get_ai_adapter" not in source


def test_turn_service_factory_uses_provider_turn_adapter_boundary():
    import app.ai.turn as turn_module

    source = inspect.getsource(turn_module.get_ai_turn_service)
    authority_source = inspect.getsource(turn_module._ai_authority_is_current)
    assert "get_provider_turn_adapter()" in source
    assert "get_ai_adapter" not in source
    assert "ai_may_reply" in authority_source
    assert "expected_ownership_version=context.expected_ownership_version" in (
        authority_source
    )
