"""Real-M1 AI-5B2 bridge checks in the runner-owned PostgreSQL cluster."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.adapters.ai.deepseek_adapter import DeepSeekAdapter, _DeepSeekHTTPTransport
from app.ai.canary_bridge import (
    AI5B2_CANARIES,
    AI5B2BudgetProfile,
    CanaryBridgeEvidence,
    CanaryCaseEvidence,
    CanaryManualReviewStatus,
    CanaryProviderMode,
    CanaryTranscriptEntry,
    CumulativeBudgetProvider,
)
from app.ai.offline_certification import (
    EvaluationDeadlineAdapter,
    OfflineBudgetLedger,
    RecordingScriptedProvider,
    ScriptedProviderStep,
    SystemEvaluationClock,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderUsage,
)

from test_ai5b1_offline_certification_postgres import (
    _assert_protected_unchanged,
    _continuation,
    _finalizer,
    _install_closed_runtime,
    _run_m1,
    _stored_state,
    _tool_result,
    read_commercial_state_for_test,
)

pytest_plugins = ("test_ai5b1_offline_certification_postgres",)


def _usage(index: int) -> ProviderUsage:
    return ProviderUsage(
        input_tokens=80 + index,
        output_tokens=20,
        total_tokens=100 + index,
        cache_hit_tokens=10,
        cache_miss_tokens=70 + index,
        reasoning_tokens=5,
    )


def _measured(result: ProviderTurnResult, index: int) -> ProviderTurnResult:
    return result.model_copy(update={"usage": _usage(index)})


def _case_evidence(
    *,
    case_index: int,
    fixture_snapshot: dict[str, str],
    messages,
    tools: tuple[str, ...],
    freshness_verified: bool,
    audits,
    tickets,
    ownership: tuple[str, int, str],
    finish_reasons: tuple[ProviderFinishReason, ...],
) -> CanaryCaseEvidence:
    spec = AI5B2_CANARIES[case_index]
    return CanaryCaseEvidence(
        case_id=spec.case_id,
        fixture_snapshot=fixture_snapshot,
        transcript=tuple(
            CanaryTranscriptEntry(direction=item.direction, content=item.content)
            for direction in ("inbound", "outbound")
            for item in messages
            if item.direction == direction
        ),
        validated_tools=tools,
        freshness_verified=freshness_verified,
        persistence={
            "message_count": len(messages),
            "audit_count": len(audits),
            "ticket_count": len(tickets),
            "owner_type": ownership[0],
            "ownership_version": ownership[1],
            "ai_execution_state": ownership[2],
        },
        finish_reasons=finish_reasons,
        deterministic_status="passed",
        manual_review_status=CanaryManualReviewStatus.pending,
        requires_drc_fluent_review=spec.requires_drc_fluent_review,
    )


@pytest.mark.asyncio
async def test_four_frozen_canaries_traverse_real_m1_and_postgres(
    postgres, monkeypatch
) -> None:
    factory, truth, baseline = postgres
    import app.modules.product_offer.service as offer_service

    validated_tools: list[str] = []
    terminal_refreshes: list[uuid.UUID] = []
    original_require_offer = offer_service.require_product_offer

    async def record_terminal_refresh(session, sellable_item_id):
        terminal_refreshes.append(sellable_item_id)
        return await original_require_offer(session, sellable_item_id)

    monkeypatch.setattr(offer_service, "require_product_offer", record_terminal_refresh)

    def c01_read(_request: ProviderTurnRequest) -> ProviderTurnResult:
        validated_tools.append("search_products")
        return _measured(
            ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="b2_c01_offer",
                        capability_name="search_products",
                        arguments={"query": "air fryer"},
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
                continuation_state=_continuation("b2_c01_offer"),
            ),
            1,
        )

    def c01_ground(request: ProviderTurnRequest) -> ProviderTurnResult:
        offer = _tool_result(request, "b2_c01_offer")
        assert offer.status == "success" and offer.output is not None
        items = offer.output["items"]
        assert isinstance(items, list) and len(items) == 1
        item = items[0]
        assert item["sellable_item_id"] == str(truth.available_item_id)
        assert item["current_usd_price"] == "55.00"
        assert item["availability"] == "available"
        assert item["offer_status"] == "sellable_now"
        return _measured(
            _finalizer(
                "b2_c01_state",
                "Le modèle 6L coûte 55 USD, est disponible et vendable maintenant.",
                selected_item_id=truth.available_item_id,
            ),
            2,
        )

    def c02_handoff(_request: ProviderTurnRequest) -> ProviderTurnResult:
        validated_tools.append("request_human_handoff")
        return _measured(
            ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="b2_c02_handoff",
                        capability_name="request_human_handoff",
                        arguments={
                            "reason_category": "qualified_purchase_intent",
                            "selected_sellable_item_id": str(truth.available_item_id),
                            "purchase_intent": "ready",
                        },
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
                continuation_state=_continuation("b2_c02_handoff"),
            ),
            3,
        )

    def c03_read(_request: ProviderTurnRequest) -> ProviderTurnResult:
        validated_tools.append("get_product_details")
        return _measured(
            ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="b2_c03_offer",
                        capability_name="get_product_details",
                        arguments={"sellable_item_id": str(truth.unavailable_item_id)},
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
                continuation_state=_continuation("b2_c03_offer"),
            ),
            4,
        )

    def c03_ground(request: ProviderTurnRequest) -> ProviderTurnResult:
        offer = _tool_result(request, "b2_c03_offer")
        assert offer.status == "success" and offer.output is not None
        product = offer.output["product"]
        assert product["sellable_item_id"] == str(truth.unavailable_item_id)
        assert product["availability"] == "out_of_stock"
        assert product["offer_status"] == "out_of_stock"
        return _measured(
            _finalizer(
                "b2_c03_state",
                "Le modèle 8L est en rupture de stock; je ne peux pas le déclarer disponible.",
            ),
            5,
        )

    def c04_read(_request: ProviderTurnRequest) -> ProviderTurnResult:
        validated_tools.append("search_products")
        return _measured(
            ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="b2_c04_offer",
                        capability_name="search_products",
                        arguments={
                            "query": "air fryer",
                            "max_budget": 45,
                            "budget_currency": "USD",
                        },
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
                continuation_state=_continuation("b2_c04_offer"),
            ),
            6,
        )

    def c04_ground(request: ProviderTurnRequest) -> ProviderTurnResult:
        offer = _tool_result(request, "b2_c04_offer")
        assert offer.status == "success" and offer.output is not None
        assert offer.output["items"] == []
        return _measured(
            ProviderTurnResult(
                tool_calls=(
                    ProviderToolCall(
                        call_id="b2_c04_state",
                        capability_name="propose_commercial_state_update",
                        arguments={
                            "response_text": (
                                "Na budget ya 45 dollars, option moins chère ezali te "
                                "na offre actuelle."
                            ),
                            "state_update": {
                                "current_goal": "Trouver un air fryer moins cher",
                                "decision_constraints": [
                                    {"kind": "budget", "value": "45 USD"}
                                ],
                                "purchase_intent": "considering",
                                "next_objective": "retrieve_options",
                            },
                        },
                    ),
                ),
                finish_reason=ProviderFinishReason.tool_call,
            ),
            7,
        )

    profile = AI5B2BudgetProfile()
    ledger = OfflineBudgetLedger(profile.offline_limits())
    reservation = {
        "reserved_tokens": profile.reserved_tokens_per_call,
        "reserved_cost_usd": profile.fixture_reserved_cost_per_call,
    }
    adapter = RecordingScriptedProvider(
        tuple(
            ScriptedProviderStep(step, **reservation)
            for step in (
                c01_read,
                c01_ground,
                c02_handoff,
                c03_read,
                c03_ground,
                c04_read,
                c04_ground,
            )
        ),
        budget=ledger,
    )
    await _install_closed_runtime(monkeypatch, factory, adapter)
    started = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)

    await _run_m1(
        phone="+243810006201",
        content=AI5B2_CANARIES[0].customer_message,
        timestamp=started,
    )

    duplicate_id = uuid.uuid4()
    duplicate_wa_id = f"b2-c02-{duplicate_id}"
    first_handoff, _, _ = await _run_m1(
        phone="+243810006202",
        content=AI5B2_CANARIES[1].customer_message,
        message_id=duplicate_id,
        whatsapp_message_id=duplicate_wa_id,
        timestamp=started + timedelta(minutes=1),
    )
    calls_before_replay = len(adapter.requests)
    replay, _, _ = await _run_m1(
        phone="+243810006202",
        content=AI5B2_CANARIES[1].customer_message,
        message_id=duplicate_id,
        whatsapp_message_id=duplicate_wa_id,
        timestamp=started + timedelta(minutes=1),
    )
    assert first_handoff["status"] == "waiting_for_human"
    assert replay["status"] == "duplicate_ignored"
    assert len(adapter.requests) == calls_before_replay
    assert terminal_refreshes == [truth.available_item_id]

    await _run_m1(
        phone="+243810006203",
        content=AI5B2_CANARIES[2].customer_message,
        timestamp=started + timedelta(minutes=2),
    )
    await _run_m1(
        phone="+243810006204",
        content=AI5B2_CANARIES[3].customer_message,
        timestamp=started + timedelta(minutes=3),
    )

    case_evidence: list[CanaryCaseEvidence] = []
    phones = [f"+24381000620{index}" for index in range(1, 5)]
    tool_slices = (
        ("search_products",),
        ("request_human_handoff",),
        ("get_product_details",),
        ("search_products",),
    )
    finish_slices = ((0, 2), (2, 3), (3, 5), (5, 7))
    for index, phone in enumerate(phones):
        conversation, messages, audits, tickets = await _stored_state(factory, phone)
        inbound = [item for item in messages if item.direction == "inbound"]
        outbound = [item for item in messages if item.direction == "outbound"]
        if index == 0:
            assert "55 USD" in outbound[0].content
            assert "disponible" in outbound[0].content
        elif index == 1:
            assert len(messages) == 2 and len(audits) == 1 and len(tickets) == 1
            assert "Rien n'est encore confirmé." in outbound[0].content
        elif index == 2:
            assert "rupture" in outbound[0].content
            assert "disponible maintenant" not in outbound[0].content.lower()
            assert "plus tard" not in outbound[0].content.lower()
        else:
            assert AI5B2_CANARIES[3].customer_message == inbound[0].content
            assert "45 dollars" in outbound[0].content
            state = await read_commercial_state_for_test(
                factory, conversation.conversation_id
            )
            assert state is not None
            assert state.decision_constraints[0].value == "45 USD"
        start, end = finish_slices[index]
        case_evidence.append(
            _case_evidence(
                case_index=index,
                fixture_snapshot={
                    "P6_sellable_item_id": str(truth.available_item_id),
                    "P8_sellable_item_id": str(truth.unavailable_item_id),
                    "P6_price_usd": "55.00",
                    "P8_availability": "out_of_stock",
                },
                messages=messages,
                tools=tool_slices[index],
                freshness_verified=True,
                audits=audits,
                tickets=tickets,
                ownership=(
                    conversation.owner_type,
                    conversation.ownership_version,
                    conversation.ai_execution_state,
                ),
                finish_reasons=tuple(
                    item.finish_reason
                    for item in adapter.evidence[start:end]
                    if item.finish_reason is not None
                ),
            )
        )

    report = CanaryBridgeEvidence(
        evidence_label=CanaryProviderMode.offline_scripted,
        provider="scripted",
        model="offline-ai5b1",
        reasoning_profile=ProviderReasoningProfile.default,
        configured_provider_deadline_seconds=12,
        configured_outer_watchdog_seconds=60,
        configured_stage_ceiling_seconds=600,
        reserved_provider_calls=ledger.provider_calls,
        reserved_tokens=ledger.reserved_tokens,
        reserved_cost_usd=ledger.reserved_cost_usd,
        observed_total_tokens=ledger.observed_tokens,
        cases=tuple(case_evidence),
        overall_decision="offline_bridge_validated",
    )
    serialized = report.redacted_json()
    assert '"evidence_label":"offline_scripted"' in serialized
    assert len(adapter.requests) == 7 and ledger.provider_calls == 7
    assert ledger.reserved_tokens == 7 * profile.reserved_tokens_per_call
    assert ledger.reserved_cost_usd == (
        Decimal(7) * profile.fixture_reserved_cost_per_call
    )
    assert ledger.observed_tokens == sum(100 + index for index in range(1, 8))
    assert adapter.network_calls == 0
    assert validated_tools == [
        "search_products",
        "request_human_handoff",
        "get_product_details",
        "search_products",
    ]
    assert all(case.manual_review_status == "pending" for case in report.cases)
    assert report.cases[-1].requires_drc_fluent_review is True
    await _assert_protected_unchanged(factory, baseline)


@pytest.mark.asyncio
async def test_deepseek_adapter_mocked_http_continues_through_real_application(
    postgres, monkeypatch
) -> None:
    factory, truth, baseline = postgres
    payloads: list[dict] = []
    hidden_reasoning = "AI5B2_MOCKED_HTTP_HIDDEN_REASONING"

    def provider_response(index: int) -> dict:
        if index == 0:
            tool_name = "search_products"
            arguments = {"query": "air fryer"}
            call_id = "b2_mock_offer"
        else:
            tool_name = "propose_commercial_state_update"
            arguments = {
                "response_text": (
                    "Le modèle 6L coûte 55 USD et est disponible maintenant."
                ),
                "state_update": {
                    "current_goal": "Vérifier le modèle 6L",
                    "selected_sellable_item_ids": [str(truth.available_item_id)],
                    "purchase_intent": "considering",
                    "next_objective": "clarify_choice",
                },
            }
            call_id = "b2_mock_state"
        return {
            "id": f"chatcmpl_b2_mock_{index}",
            "object": "chat.completion",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": hidden_reasoning,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": 90 + index,
                "completion_tokens": 20,
                "total_tokens": 110 + index,
                "prompt_cache_hit_tokens": 10,
                "prompt_cache_miss_tokens": 80 + index,
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        }

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        payloads.append(payload)
        return httpx.Response(200, json=provider_response(len(payloads) - 1))

    mock_http = httpx.MockTransport(handler)
    transport = _DeepSeekHTTPTransport(
        api_key="inert-mocked-http-placeholder",
        timeout_s=12,
        http_transport=mock_http,
    )
    deepseek = DeepSeekAdapter(
        api_key="inert-mocked-http-placeholder",
        transport=transport,
    )
    profile = AI5B2BudgetProfile()
    ledger = OfflineBudgetLedger(profile.offline_limits())
    budgeted = CumulativeBudgetProvider(deepseek, ledger=ledger, profile=profile)
    controller = EvaluationDeadlineAdapter(
        budgeted,
        clock=SystemEvaluationClock(),
        deadline_seconds=12,
    )
    await _install_closed_runtime(
        monkeypatch,
        factory,
        controller,
        allow_mocked_provider_http=True,
    )

    result, _, _ = await _run_m1(
        phone="+243810006205",
        content=AI5B2_CANARIES[0].customer_message,
        timestamp=datetime(2026, 9, 2, 9, tzinfo=timezone.utc),
    )
    conversation, messages, audits, tickets = await _stored_state(
        factory, "+243810006205"
    )
    state = await read_commercial_state_for_test(factory, conversation.conversation_id)

    assert result["status"] == "processed"
    assert len(payloads) == 2 and budgeted.dispatched_requests == 2
    assert ledger.provider_calls == 2 and ledger.observed_tokens == 221
    assert payloads[0]["max_tokens"] == 512
    assert payloads[0]["thinking"] == {"type": "enabled"}
    assert payloads[0]["reasoning_effort"] == "high"
    assert any(message.get("role") == "tool" for message in payloads[1]["messages"])
    assert any(
        message.get("role") == "assistant" and message.get("tool_calls")
        for message in payloads[1]["messages"]
    )
    assert len(messages) == 2 and len(audits) == 1 and tickets == []
    outbound = [item for item in messages if item.direction == "outbound"]
    assert "55 USD" in outbound[0].content
    assert state is not None
    assert state.selected_sellable_item_ids == [truth.available_item_id]
    persisted = json.dumps(
        {
            "messages": [item.content for item in messages],
            "audits": [item.safe_code for item in audits],
            "state": state.model_dump(mode="json"),
        },
        default=str,
    )
    assert hidden_reasoning not in persisted
    assert controller.unfinished_task_count == 0
    assert controller.deadline_evidence == []
    mock_case = _case_evidence(
        case_index=0,
        fixture_snapshot={
            "P6_sellable_item_id": str(truth.available_item_id),
            "P6_price_usd": "55.00",
        },
        messages=messages,
        tools=("search_products",),
        freshness_verified=True,
        audits=audits,
        tickets=tickets,
        ownership=(
            conversation.owner_type,
            conversation.ownership_version,
            conversation.ai_execution_state,
        ),
        finish_reasons=tuple(result.finish_reason for result in budgeted.results),
    )
    mock_report = CanaryBridgeEvidence(
        evidence_label=CanaryProviderMode.offline_mocked_http,
        provider=deepseek.provider_name,
        model=deepseek.model,
        reasoning_profile=ProviderReasoningProfile.default,
        returned_provider="deepseek",
        returned_model=provider_response(0)["model"],
        configured_provider_deadline_seconds=12,
        configured_outer_watchdog_seconds=60,
        configured_stage_ceiling_seconds=600,
        reserved_provider_calls=ledger.provider_calls,
        reserved_tokens=ledger.reserved_tokens,
        reserved_cost_usd=ledger.reserved_cost_usd,
        observed_total_tokens=ledger.observed_tokens,
        cases=(mock_case,),
        overall_decision="offline_bridge_validated",
    )
    mock_serialized = mock_report.redacted_json()
    assert '"evidence_label":"offline_mocked_http"' in mock_serialized
    assert hidden_reasoning not in mock_serialized
    await _assert_protected_unchanged(factory, baseline)
