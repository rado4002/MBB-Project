import inspect

import pytest

from app.ai.policy import AI_SYSTEM_POLICY_VERSION, get_system_policy
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderMessage,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService


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
            )
        )


def test_turn_requires_a_positive_ownership_generation():
    with pytest.raises(ValueError, match="ownership version"):
        AITurn(
            user_content="Mbote",
            language="french",
            expected_ownership_version=0,
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
    assert "get_provider_turn_adapter()" in source
    assert "get_ai_adapter" not in source
