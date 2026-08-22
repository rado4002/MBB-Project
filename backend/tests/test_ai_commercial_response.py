from __future__ import annotations

import json
import uuid

import pytest

from app.ai.commercial_response import (
    CommercialResponseError,
    commercial_response_fallback,
    commercial_response_mode,
    parse_commercial_response_plan,
    validate_and_render_commercial_response,
)
from app.ai.audit import AITurnOutcome
from app.ai.capabilities import (
    CapabilityDefinition,
    CapabilityRegistry,
    SearchProductsInput,
    SearchProductsOutput,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderToolCall,
    ProviderToolResult,
    ProviderTurnResult,
)
from app.ai.turn import AITurn, AITurnService

PRODUCT_4L = uuid.UUID("11111111-1111-4111-8111-111111111111")
PRODUCT_6L = uuid.UUID("22222222-2222-4222-8222-222222222222")
PRODUCT_8L = uuid.UUID("33333333-3333-4333-8333-333333333333")


def _item(
    product_ref: uuid.UUID,
    model: str,
    price: str | None,
    *,
    availability: str = "available",
    sellable: bool = True,
) -> dict[str, object]:
    return {
        "product_id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"product-{model}")),
        "sellable_item_id": str(product_ref),
        "name": f"MBB Air Fryer {model}",
        "model_label": model,
        "category_code": "air_fryer",
        "attributes": {"capacity_liters": int(model.removesuffix("L"))},
        "current_usd_price": price,
        "price_currency": "USD",
        "cdf_quote_status": "available",
        "derived_cdf_quote": {"currency": "CDF", "amount": "154000"},
        "availability": availability,
        "offer_status": "sellable_now" if sellable else "out_of_stock",
        "is_sellable_now": sellable,
        "primary_media": None,
    }


def _search_evidence(
    *items: dict[str, object],
    budget: str | None = "70",
    currency: str = "USD",
) -> tuple[tuple[ProviderToolCall, ...], tuple[ProviderToolResult, ...]]:
    arguments: dict[str, object] = {
        "query": "air fryer",
        "budget_currency": currency,
        "search_mode": "INCLUDE_UNAVAILABLE",
        "limit": 5,
    }
    if budget is not None:
        arguments["max_budget"] = budget
    call = ProviderToolCall(
        call_id="call_search",
        capability_name="search_products",
        arguments=arguments,
    )
    result = ProviderToolResult(
        call_id=call.call_id,
        capability_name=call.capability_name,
        status="success",
        output={"items": list(items)},
    )
    return (call,), (result,)


def _details_evidence() -> tuple[tuple[ProviderToolCall, ...], tuple[ProviderToolResult, ...]]:
    call = ProviderToolCall(
        call_id="call_details",
        capability_name="get_product_details",
        arguments={"sellable_item_id": str(PRODUCT_6L)},
    )
    product = _item(PRODUCT_6L, "6L", "55") | {
        "description": "Compact verified cooking details.",
        "sku": "AF-6L",
    }
    result = ProviderToolResult(
        call_id=call.call_id,
        capability_name=call.capability_name,
        status="success",
        output={"product": product},
    )
    return (call,), (result,)


def _plan(**overrides: object) -> str:
    values: dict[str, object] = {
        "response_kind": "PRODUCT_LIST",
        "product_refs": [str(PRODUCT_4L), str(PRODUCT_6L)],
        "fact_fields": ["NAME", "CURRENT_PRICE", "CURRENT_AVAILABILITY"],
        "recommendation_ref": None,
        "recommendation_reason": "NONE",
        "next_action": "COMPARE_PRODUCTS",
        "clarification": "NONE",
        "tone": "CONCISE",
    }
    values.update(overrides)
    return json.dumps(values)


def _render(
    plan: str,
    *,
    language: str = "english",
    calls: tuple[ProviderToolCall, ...] | None = None,
    results: tuple[ProviderToolResult, ...] | None = None,
    exposed: tuple[str, ...] = (
        "get_product_details",
        "request_human_handoff",
        "search_products",
    ),
) -> str:
    if calls is None or results is None:
        calls, results = _search_evidence(
            _item(PRODUCT_4L, "4L", "45"),
            _item(PRODUCT_6L, "6L", "55"),
        )
    return validate_and_render_commercial_response(
        plan,
        language=language,
        exposed_capabilities=exposed,
        tool_calls=calls,
        tool_results=results,
    )


def test_commercial_mode_is_limited_to_existing_product_capabilities() -> None:
    assert commercial_response_mode(("search_products",)) is True
    assert commercial_response_mode(("get_product_details",)) is True
    assert commercial_response_mode(("request_human_handoff",)) is False
    assert commercial_response_mode(("unrelated_tool",)) is False


def test_valid_product_list_and_comparison_render_authoritative_values() -> None:
    product_list = _render(_plan())
    comparison = _render(_plan(response_kind="COMPARISON"))

    for text in (product_list, comparison):
        assert "MBB Air Fryer 4L — 45 USD, available now" in text
        assert "MBB Air Fryer 6L — 55 USD, available now" in text
        assert "compare these products" in text


def test_valid_details_render_only_successful_detail_output() -> None:
    calls, results = _details_evidence()
    text = _render(
        _plan(
            response_kind="PRODUCT_DETAILS",
            product_refs=[str(PRODUCT_6L)],
            fact_fields=["NAME", "MODEL", "APPROVED_PRODUCT_DETAIL"],
            next_action="NONE",
        ),
        calls=calls,
        results=results,
    )

    assert text == "MBB Air Fryer 6L — Compact verified cooking details."


@pytest.mark.parametrize("reason", ["BUDGET_FIT", "AVAILABLE_NOW"])
def test_valid_recommendation_reasons_require_authoritative_proof(reason: str) -> None:
    text = _render(
        _plan(
            response_kind="RECOMMENDATION",
            recommendation_ref=str(PRODUCT_6L),
            recommendation_reason=reason,
            next_action="SHOW_DETAILS",
        )
    )

    assert "I recommend MBB Air Fryer 6L — 55 USD, available now." in text
    assert "verified details" in text


def test_budget_fit_rejects_product_outside_executed_search_budget() -> None:
    calls, results = _search_evidence(_item(PRODUCT_6L, "6L", "55"), budget="50")
    with pytest.raises(CommercialResponseError, match="commercial_plan_invalid"):
        _render(
            _plan(
                response_kind="RECOMMENDATION",
                product_refs=[str(PRODUCT_6L)],
                recommendation_ref=str(PRODUCT_6L),
                recommendation_reason="BUDGET_FIT",
                next_action="NONE",
            ),
            calls=calls,
            results=results,
        )


def test_available_now_rejects_out_of_stock_product_and_renders_truth() -> None:
    calls, results = _search_evidence(
        _item(
            PRODUCT_8L,
            "8L",
            "65",
            availability="out_of_stock",
            sellable=False,
        ),
        budget=None,
    )
    with pytest.raises(CommercialResponseError, match="commercial_plan_invalid"):
        _render(
            _plan(
                response_kind="RECOMMENDATION",
                product_refs=[str(PRODUCT_8L)],
                recommendation_ref=str(PRODUCT_8L),
                recommendation_reason="AVAILABLE_NOW",
                next_action="NONE",
            ),
            calls=calls,
            results=results,
        )

    text = _render(
        _plan(
            product_refs=[str(PRODUCT_8L)],
            fact_fields=["NAME", "CURRENT_AVAILABILITY", "CURRENT_SELLABILITY"],
            next_action="SEARCH_MORE",
        ),
        calls=calls,
        results=results,
    )
    assert "out of stock, not currently sellable" in text


def test_available_stock_never_hides_non_sellable_state() -> None:
    calls, results = _search_evidence(
        _item(PRODUCT_6L, "6L", "55", availability="available", sellable=False),
        budget=None,
    )

    text = _render(
        _plan(
            product_refs=[str(PRODUCT_6L)],
            fact_fields=["NAME", "CURRENT_AVAILABILITY"],
            next_action="NONE",
        ),
        calls=calls,
        results=results,
    )

    assert "stock reported available, but not currently sellable" in text


@pytest.mark.parametrize(
    ("plan", "code"),
    [
        (_plan(product_refs=[str(uuid.uuid4())]), "commercial_plan_unknown_product"),
        (
            _plan(
                response_kind="PRODUCT_DETAILS",
                product_refs=[str(PRODUCT_4L)],
                fact_fields=["NAME", "APPROVED_PRODUCT_DETAIL"],
                next_action="NONE",
            ),
            "commercial_plan_missing_fact",
        ),
        (
            _plan(next_action="REQUEST_HUMAN_HANDOFF"),
            "commercial_plan_unsupported_action",
        ),
    ],
)
def test_unknown_product_missing_fact_and_unexposed_action_fail_closed(
    plan: str,
    code: str,
) -> None:
    with pytest.raises(CommercialResponseError, match=code):
        _render(plan, exposed=("search_products", "get_product_details"))


def test_product_from_absent_or_failed_tool_result_is_unknown() -> None:
    failed = ProviderToolResult(
        call_id="call_search",
        capability_name="search_products",
        status="error",
        error={"category": "execution_failed"},
    )
    calls, _ = _search_evidence(_item(PRODUCT_4L, "4L", "45"))
    with pytest.raises(CommercialResponseError, match="commercial_plan_unknown_product"):
        _render(_plan(product_refs=[str(PRODUCT_4L)]), calls=calls, results=(failed,))


@pytest.mark.parametrize(
    "extra",
    [
        {"price": 1},
        {"reservation": True},
        {"payment_status": "accepted"},
        {"delivery_date": "tomorrow"},
        {"future_availability": "tomorrow"},
    ],
)
def test_copied_or_unsupported_commercial_values_cannot_enter_plan(extra: dict) -> None:
    with pytest.raises(CommercialResponseError, match="commercial_plan_invalid"):
        parse_commercial_response_plan(_plan(**extra))


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "```json\n{}\n```",
        'Here is the plan: {"response_kind":"CLARIFY"}',
        '{"response_kind":"CLARIFY"} trailing prose',
        '{"response_kind":',
        _plan(response_kind="recommendation"),
        _plan(response_kind=None),
        _plan(next_action="RESERVE"),
        _plan(recommendation_reason="FEATURE_FIT"),
        _plan(unknown_field="value"),
    ],
)
def test_json_parser_is_strict_and_never_extracts_substrings(raw: str) -> None:
    with pytest.raises(CommercialResponseError, match="commercial_plan_invalid"):
        parse_commercial_response_plan(raw)


@pytest.mark.parametrize(
    "overrides",
    [
        {"product_refs": [str(PRODUCT_4L), str(PRODUCT_4L)]},
        {
            "product_refs": [str(uuid.uuid4()) for _ in range(6)],
            "fact_fields": ["NAME"],
        },
        {"fact_fields": ["NAME", "NAME"]},
        {"response_kind": "CLARIFY", "clarification": "NONE"},
        {"response_kind": "PRODUCT_DETAILS"},
    ],
)
def test_duplicate_excess_and_missing_shape_fields_are_rejected(overrides: dict) -> None:
    with pytest.raises(CommercialResponseError, match="commercial_plan_invalid"):
        parse_commercial_response_plan(_plan(**overrides))


def test_no_match_requires_a_successful_empty_search() -> None:
    plan = _plan(
        response_kind="NO_MATCH",
        product_refs=[],
        fact_fields=[],
        next_action="SEARCH_MORE",
    )
    calls, nonempty = _search_evidence(_item(PRODUCT_4L, "4L", "45"))
    with pytest.raises(CommercialResponseError, match="commercial_plan_invalid"):
        _render(plan, calls=calls, results=nonempty)

    calls, empty = _search_evidence(budget=None)
    assert _render(plan, calls=calls, results=empty).startswith("I found no matching")


def test_unsupported_request_has_no_unsupported_action_claim() -> None:
    text = _render(
        _plan(
            response_kind="UNSUPPORTED_COMMERCIAL_REQUEST",
            product_refs=[],
            fact_fields=[],
            next_action="SEARCH_MORE",
        )
    )
    assert text == (
        "That commercial request is not supported here. "
        "I can search for more current options."
    )
    assert "reserve" not in text.casefold()
    assert "payment" not in text.casefold()


def test_handoff_next_action_is_only_an_offer_when_capability_is_exposed() -> None:
    text = _render(
        _plan(
            response_kind="UNSUPPORTED_COMMERCIAL_REQUEST",
            product_refs=[],
            fact_fields=[],
            next_action="REQUEST_HUMAN_HANDOFF",
        )
    )

    assert text.endswith("I can ask a human adviser to take over.")
    assert "transferred" not in text.casefold()


def test_french_renderer_uses_authoritative_values() -> None:
    text = _render(_plan(next_action="SHOW_DETAILS"), language="french")

    assert "Options vérifiées" in text
    assert "45 USD, disponible maintenant" in text
    assert "détails vérifiés" in text


def test_lingala_uses_existing_safe_fallback_until_native_review() -> None:
    with pytest.raises(
        CommercialResponseError,
        match="commercial_plan_language_review_required",
    ):
        _render(_plan(), language="lingala")
    fallback = commercial_response_fallback("lingala")
    assert fallback != "error_fallback"
    assert "problème" in fallback


class _SequenceAdapter:
    def __init__(self, *results: ProviderTurnResult) -> None:
        self.results = list(results)
        self.calls = []

    async def generate_turn(self, request):
        self.calls.append(request)
        return self.results.pop(0)


async def _authority_allowed(_context) -> bool:
    return True


def _search_registry(handler) -> CapabilityRegistry:
    return CapabilityRegistry(
        (
            CapabilityDefinition(
                name="search_products",
                description="Search authoritative products.",
                input_model=SearchProductsInput,
                output_model=SearchProductsOutput,
                handler=handler,
            ),
        )
    )


def _commercial_turn(*, language: str = "french") -> AITurn:
    return AITurn(
        user_content="Je cherche une friteuse à air.",
        language=language,
        expected_ownership_version=1,
        conversation_id=uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"),
        allowed_capabilities=("search_products",),
    )


@pytest.mark.asyncio
async def test_turn_uses_same_continuation_and_never_releases_provider_plan() -> None:
    executions = 0

    async def handler(_context, _arguments):
        nonlocal executions
        executions += 1
        return SearchProductsOutput.model_validate_json(
            json.dumps({"items": [_item(PRODUCT_6L, "6L", "55")]})
        )

    registry = _search_registry(handler)
    tool_call = ProviderToolCall(
        call_id="call_search",
        capability_name="search_products",
        arguments={
            "query": "friteuse à air",
            "max_budget": "70",
            "budget_currency": "USD",
            "search_mode": "SELLABLE_ONLY",
            "limit": 5,
        },
    )
    raw_plan = _plan(
        response_kind="RECOMMENDATION",
        product_refs=[str(PRODUCT_6L)],
        recommendation_ref=str(PRODUCT_6L),
        recommendation_reason="BUDGET_FIT",
        next_action="SEARCH_MORE",
    )
    adapter = _SequenceAdapter(
        ProviderTurnResult(
            tool_calls=(tool_call,),
            finish_reason=ProviderFinishReason.tool_call,
        ),
        ProviderTurnResult(
            text=raw_plan,
            finish_reason=ProviderFinishReason.completed,
        ),
    )
    service = AITurnService(
        adapter,
        capability_registry=registry,
        authority_checker=_authority_allowed,
    )

    finalized = await service.generate_finalized(_commercial_turn())

    assert len(adapter.calls) == 2
    assert executions == 1
    assert finalized.audit_record.outcome == AITurnOutcome.response_generated
    assert finalized.text is not None
    assert finalized.text != raw_plan
    assert "55 USD, disponible maintenant" in finalized.text
    assert "CommercialResponsePlan" not in finalized.text
    assert "output exactly one JSON object" in adapter.calls[0].system_instruction
    assert json.loads(adapter.calls[1].messages[-1].content)["status"] == "success"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provider_text",
    [
        "Your payment was accepted.",
        'Prose {"response_kind":"UNSUPPORTED_COMMERCIAL_REQUEST"}',
        "```json\n{}\n```",
    ],
)
async def test_turn_fails_closed_without_retry_or_raw_text_bypass(
    provider_text: str,
) -> None:
    async def unused_handler(_context, _arguments):
        return SearchProductsOutput(items=[])

    adapter = _SequenceAdapter(
        ProviderTurnResult(
            text=provider_text,
            finish_reason=ProviderFinishReason.completed,
        )
    )
    service = AITurnService(
        adapter,
        capability_registry=_search_registry(unused_handler),
        authority_checker=_authority_allowed,
    )

    finalized = await service.generate_finalized(_commercial_turn())

    assert len(adapter.calls) == 1
    assert finalized.text == commercial_response_fallback("french")
    assert provider_text not in finalized.text
    assert finalized.audit_record.outcome == AITurnOutcome.fallback_used
    assert finalized.audit_record.safe_code == "commercial_plan_invalid"


@pytest.mark.asyncio
async def test_turn_lingala_plan_uses_safe_existing_fallback() -> None:
    async def unused_handler(_context, _arguments):
        return SearchProductsOutput(items=[])

    raw_plan = _plan(
        response_kind="CLARIFY",
        product_refs=[],
        fact_fields=[],
        next_action="NONE",
        clarification="ASK_BUDGET",
    )
    adapter = _SequenceAdapter(
        ProviderTurnResult(
            text=raw_plan,
            finish_reason=ProviderFinishReason.completed,
        )
    )
    service = AITurnService(
        adapter,
        capability_registry=_search_registry(unused_handler),
        authority_checker=_authority_allowed,
    )

    finalized = await service.generate_finalized(_commercial_turn(language="lingala"))

    assert len(adapter.calls) == 1
    assert finalized.text == commercial_response_fallback("lingala")
    assert finalized.audit_record.outcome == AITurnOutcome.fallback_used
    assert finalized.audit_record.safe_code == (
        "commercial_plan_language_review_required"
    )
