from __future__ import annotations

import inspect
import uuid
from types import SimpleNamespace

import pytest
from pydantic import Field

from app.ai.capabilities import (
    AI_CAPABILITY_REGISTRY,
    CapabilityDefinition,
    CapabilityErrorCategory,
    CapabilityExecutor,
    CapabilityFailure,
    CapabilityRegistry,
    CapabilitySuccess,
    DuplicateCapabilityName,
    SafeCapabilityError,
    StrictCapabilityModel,
    TrustedCapabilityContext,
)


class _EchoInput(StrictCapabilityModel):
    text: str = Field(min_length=1, max_length=40)
    count: int = Field(ge=1, le=3)


class _EchoOutput(StrictCapabilityModel):
    value: str = Field(min_length=1, max_length=120)


def _definition(name, handler):
    return CapabilityDefinition(
        name=name,
        description=f"Test-only {name} capability.",
        input_model=_EchoInput,
        output_model=_EchoOutput,
        handler=handler,
    )


def _context() -> TrustedCapabilityContext:
    return TrustedCapabilityContext(
        conversation_id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        expected_ownership_version=3,
    )


def test_registry_is_explicit_resolvable_and_rejects_duplicates():
    async def handler(_context, arguments):
        return {"value": arguments.text}

    definition = _definition("tool_a", handler)
    registry = CapabilityRegistry((definition,))

    assert registry.resolve("tool_a") is definition
    assert registry.resolve("not_registered") is None
    with pytest.raises(DuplicateCapabilityName):
        CapabilityRegistry((definition, definition))


@pytest.mark.asyncio
async def test_unknown_tool_fails_safely():
    executor = CapabilityExecutor(CapabilityRegistry(()))

    result = await executor.execute(
        requested_name="not_registered",
        model_arguments={},
        allowed_capabilities={"not_registered"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.unknown_tool)


@pytest.mark.asyncio
async def test_model_can_request_but_cannot_grant_itself_a_capability():
    calls = {"tool_a": 0, "tool_b": 0}

    async def tool_a(_context, arguments):
        calls["tool_a"] += 1
        return {"value": arguments.text}

    async def tool_b(_context, arguments):
        calls["tool_b"] += 1
        return {"value": arguments.text}

    registry = CapabilityRegistry(
        (_definition("tool_a", tool_a), _definition("tool_b", tool_b))
    )
    executor = CapabilityExecutor(registry)
    trusted_context = _context()

    denied = await executor.execute(
        requested_name="tool_b",
        model_arguments={
            "text": "try escalation",
            "count": 1,
            "allowed_tools": ["tool_b"],
            "conversation_id": str(uuid.uuid4()),
        },
        allowed_capabilities={"tool_a"},
        context=trusted_context,
    )

    assert denied == CapabilityFailure(CapabilityErrorCategory.tool_not_allowed)
    assert calls == {"tool_a": 0, "tool_b": 0}

    injected_scope = await executor.execute(
        requested_name="tool_a",
        model_arguments={
            "text": "try scope override",
            "count": 1,
            "conversation_id": str(uuid.uuid4()),
        },
        allowed_capabilities={"tool_a"},
        context=trusted_context,
    )

    assert injected_scope == CapabilityFailure(
        CapabilityErrorCategory.invalid_arguments
    )
    assert calls == {"tool_a": 0, "tool_b": 0}


@pytest.mark.asyncio
async def test_valid_input_executes_with_separate_trusted_context():
    seen = []

    async def handler(context, arguments):
        seen.append((context, arguments))
        return {"value": arguments.text * arguments.count}

    executor = CapabilityExecutor(
        CapabilityRegistry((_definition("echo_value", handler),))
    )
    context = _context()

    result = await executor.execute(
        requested_name="echo_value",
        model_arguments={"text": "mbb", "count": 2},
        allowed_capabilities={"echo_value"},
        context=context,
    )

    assert isinstance(result, CapabilitySuccess)
    assert result.output == _EchoOutput(value="mbbmbb")
    assert seen[0][0] is context
    assert seen[0][1] == _EchoInput(text="mbb", count=2)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    (
        {"text": "missing count"},
        {"text": "wrong type", "count": "2"},
        {"text": "extra", "count": 1, "unexpected": True},
    ),
)
async def test_invalid_input_never_invokes_handler(arguments):
    calls = 0

    async def handler(_context, _arguments):
        nonlocal calls
        calls += 1
        return {"value": "should not execute"}

    executor = CapabilityExecutor(
        CapabilityRegistry((_definition("echo_value", handler),))
    )
    result = await executor.execute(
        requested_name="echo_value",
        model_arguments=arguments,
        allowed_capabilities={"echo_value"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.invalid_arguments)
    assert calls == 0


@pytest.mark.asyncio
async def test_invalid_handler_output_fails_without_returning_internal_object():
    internal = SimpleNamespace(value="internal", secret="must-not-cross-boundary")

    async def handler(_context, _arguments):
        return internal

    executor = CapabilityExecutor(
        CapabilityRegistry((_definition("echo_value", handler),))
    )
    result = await executor.execute(
        requested_name="echo_value",
        model_arguments={"text": "valid", "count": 1},
        allowed_capabilities={"echo_value"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.execution_failed)
    assert "secret" not in repr(result)
    assert "must-not-cross-boundary" not in repr(result)


@pytest.mark.asyncio
async def test_safe_and_unexpected_errors_do_not_leak_internal_details():
    async def safe_handler(_context, _arguments):
        raise SafeCapabilityError("business_conflict")

    async def broken_handler(_context, _arguments):
        raise RuntimeError("postgres://secret-host/private-database")

    registry = CapabilityRegistry(
        (
            _definition("safe_failure", safe_handler),
            _definition("broken_failure", broken_handler),
        )
    )
    executor = CapabilityExecutor(registry)
    context = _context()
    arguments = {"text": "valid", "count": 1}

    safe_result = await executor.execute(
        requested_name="safe_failure",
        model_arguments=arguments,
        allowed_capabilities={"safe_failure"},
        context=context,
    )
    broken_result = await executor.execute(
        requested_name="broken_failure",
        model_arguments=arguments,
        allowed_capabilities={"broken_failure"},
        context=context,
    )

    assert safe_result == CapabilityFailure(
        CapabilityErrorCategory.execution_failed,
        safe_code="business_conflict",
    )
    assert broken_result == CapabilityFailure(
        CapabilityErrorCategory.execution_failed
    )
    assert "secret-host" not in repr(broken_result)
    assert "private-database" not in repr(broken_result)


def test_provider_neutral_specification_exposes_only_allowed_registered_tools():
    async def handler(_context, arguments):
        return {"value": arguments.text}

    registry = CapabilityRegistry(
        (_definition("tool_a", handler), _definition("tool_b", handler))
    )

    specifications = registry.specifications({"tool_a", "unregistered"})

    assert len(specifications) == 1
    assert specifications[0].name == "tool_a"
    assert specifications[0].description == "Test-only tool_a capability."
    assert specifications[0].input_schema["additionalProperties"] is False
    assert set(specifications[0].input_schema["properties"]) == {"text", "count"}


def test_production_registry_contains_only_approved_capabilities():
    assert len(AI_CAPABILITY_REGISTRY) == 3
    assert AI_CAPABILITY_REGISTRY.specifications(set()) == ()
    specifications = AI_CAPABILITY_REGISTRY.specifications(
        {
            "get_product_details",
            "request_human_handoff",
            "search_products",
        }
    )
    assert [specification.name for specification in specifications] == [
        "get_product_details",
        "request_human_handoff",
        "search_products",
    ]
    assert set(specifications[0].input_schema["properties"]) == {
        "sellable_item_id"
    }
    assert set(specifications[1].input_schema["properties"]) == {
        "reason_category"
    }
    assert set(specifications[2].input_schema["properties"]) == {
        "query",
        "category_code",
        "max_budget",
        "budget_currency",
        "search_mode",
        "limit",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authority_argument",
    (
        "conversation_id",
        "turn_id",
        "ownership_version",
        "expected_ownership_version",
        "human_owner_account_id",
        "owner_type",
        "allowed_tools",
    ),
)
async def test_handoff_rejects_model_supplied_authority(authority_argument):
    result = await CapabilityExecutor(AI_CAPABILITY_REGISTRY).execute(
        requested_name="request_human_handoff",
        model_arguments={
            "reason_category": "customer_requested_human",
            authority_argument: "model-controlled",
        },
        allowed_capabilities={"request_human_handoff"},
        context=_context(),
    )

    assert result == CapabilityFailure(CapabilityErrorCategory.invalid_arguments)


def test_core_source_is_provider_neutral_and_has_no_dynamic_discovery():
    import app.ai.capabilities as capability_module

    source = inspect.getsource(capability_module)
    for prohibited_term in (
        "DeepSeek",
        "OpenAI",
        "Anthropic",
        "tool_call_id",
        "finish_reason",
        "entry_points",
        "import_module",
        "pkgutil",
    ):
        assert prohibited_term not in source
