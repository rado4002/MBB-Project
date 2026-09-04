"""Real-M1 AI-5B2 bridge checks in the runner-owned PostgreSQL cluster."""

from __future__ import annotations

import json
from pathlib import Path
import socket
import subprocess
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest

from app.adapters.ai.deepseek_adapter import DeepSeekAdapter, _DeepSeekHTTPTransport
from app.ai.canary_bridge import (
    AI5B2_CANARIES,
    AI5B2BudgetDecision,
    AI5B2BudgetDecisionLimits,
    AI5B2BudgetProfile,
    AI5B2ProviderSelection,
    CanaryAuthorizationRecord,
    CanaryBridgeEvidence,
    CanaryCaseEvidence,
    CanaryManualReviewStatus,
    CanaryProviderMode,
    CanaryPricingVerificationRecord,
    CanaryReviewerAssignmentRecord,
    CanaryTranscriptEntry,
    CumulativeBudgetProvider,
    conservative_json_request_reservation,
    dispatch_guarded_canary_stage,
)
from app.ai.offline_certification import (
    EvaluationDeadlineAdapter,
    OfflineBudgetLedger,
    SystemEvaluationClock,
)
from app.ai.provider_contract import (
    ProviderFinishReason,
    ProviderReasoningProfile,
)
from scripts.run_ai5b2_canary_bridge import CanaryCLIOverrides, main as bridge_main

from test_ai5b1_offline_certification_postgres import (
    _assert_protected_unchanged,
    _install_closed_runtime,
    _run_m1,
    _stored_state,
    read_commercial_state_for_test,
)

pytest_plugins = ("test_ai5b1_offline_certification_postgres",)


def _assert_cleanup_observed(cleanup: dict) -> None:
    cluster_root = Path(tempfile.gettempdir()) / cleanup["cluster_identity"]
    assert not cluster_root.exists()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", cleanup["loopback_port"]), timeout=1)


def _budget_decision_record(
    run_id: str,
    baseline: str,
    authorization_record_id: str,
) -> AI5B2BudgetDecision:
    accepted_at = datetime.now(timezone.utc)
    return AI5B2BudgetDecision(
        decision_id=f"synthetic:budget-decision:{run_id}",
        decision_version="mbb-ai5b2-budget-decision-v1",
        contract_version="mbb-ai5b-contract-v2",
        accepted=True,
        accepted_by="project-owner",
        accepted_at=accepted_at,
        valid_until=accepted_at + timedelta(hours=1),
        run_id=run_id,
        authorization_record_id=authorization_record_id,
        baseline_commit=baseline,
        limits=AI5B2BudgetDecisionLimits(
            case_executions=4,
            provider_requests=21,
            total_api_tokens=40_000,
            cost_usd=Decimal("0.05"),
            completion_tokens_per_request=512,
            automatic_provider_retries=0,
            evaluation_durable_actions=1,
            ai_turn_provider_calls=3,
            ai_turn_tool_rounds=2,
            ai_turn_capability_executions=3,
            ai_turn_durable_action_attempts=2,
            provider_deadline_seconds=12,
            provider_deadline_scope="provider_request",
            outer_watchdog_seconds=60,
            outer_watchdog_scope="evaluation_operation",
            stage_ceiling_seconds=600,
            stage_ceiling_scope="complete_four_case_stage",
        ),
        admission_method="utf8_wire_bytes_plus_json_nodes_estimate_v1",
        settle_from_complete_api_usage=True,
        retain_unresolved_estimates=True,
        stop_on_missing_or_uncertain_usage=True,
        accepts_single_dispatched_request_token_or_cost_overrun=True,
        accepts_no_subsequent_dispatch_after_overrun=True,
        synthetic=True,
    )


def _cli_arguments(tmp_path, run_id: str) -> tuple[str, ...]:
    baseline = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    authorization_record_id = f"synthetic:authorization:{run_id}"
    decision_path = tmp_path / f"{run_id}-budget-decision.json"
    decision_path.write_text(
        _budget_decision_record(
            run_id,
            baseline,
            authorization_record_id,
        ).model_dump_json(),
        encoding="utf-8",
    )
    return (
        "--live",
        "--authorize-live",
        "--run-id",
        run_id,
        "--authorized-baseline",
        baseline,
        "--authorization-record-id",
        authorization_record_id,
        "--budget-decision-file",
        str(decision_path),
        "--pricing-record-id",
        f"synthetic:pricing:{run_id}",
        "--pricing-source",
        "synthetic://offline-cli-mocked-http",
        "--pricing-verified-at",
        "synthetic-offline-verification",
        "--input-usd-per-million",
        "0.50",
        "--output-usd-per-million",
        "1.00",
        "--reviewer-record-id",
        f"synthetic:reviewer:{run_id}",
        "--reviewer-id",
        "synthetic:reviewer:drc-test-only",
        "--reviewer-drc-language-familiarity",
        "--external-effects-disabled",
        "--evidence-root",
        str(tmp_path),
    )


def _deepseek_reservation(adapter: DeepSeekAdapter):
    return lambda request: conservative_json_request_reservation(
        adapter.build_request_payload(request),
        max_output_tokens=request.max_output_tokens,
    )


def _mocked_cli_transport(
    payloads: list[dict],
    *,
    failure_index: int | None = None,
    missing_usage: bool = False,
    under_reservation_index: int | None = None,
    outcome_probe: bool = False,
):
    def build(credential, truth):
        response_tool_rounds = (
            (
                ("b2_c01_offer", "search_products", {"query": "air fryer"}),
                (
                    "b2_c01_refined_offer",
                    "search_products",
                    {
                        "query": "air fryer",
                        "max_budget": 55,
                        "budget_currency": "USD",
                    },
                ),
            ),
            (
                (
                    "b2_c01_state",
                    "propose_commercial_state_update",
                    {
                        "response_text": (
                            "Le MBB Test Air Fryer 6L coûte 55 USD, est disponible "
                            "et vendable maintenant."
                        ),
                        "state_update": {
                            "selected_sellable_item_ids": [
                                str(truth.available_item_id)
                            ],
                            "purchase_intent": "considering",
                            "next_objective": "clarify_choice",
                        },
                    },
                ),
            ),
            (
                (
                    "b2_c02_handoff",
                    "request_human_handoff",
                    {
                        "reason_category": "qualified_purchase_intent",
                        "selected_sellable_item_id": str(truth.available_item_id),
                        "purchase_intent": "ready",
                    },
                ),
                (
                    "b2_c02_after_terminal",
                    "search_products",
                    {"query": "must not execute after terminal handoff"},
                ),
            ),
            (
                (
                    "b2_c03_offer",
                    "get_product_details",
                    {"sellable_item_id": str(truth.unavailable_item_id)},
                ),
            ),
            (
                (
                    "b2_c03_state",
                    "propose_commercial_state_update",
                    {
                        "response_text": (
                            "Le modèle 8L est en rupture de stock; je ne peux pas le "
                            "déclarer disponible."
                        ),
                        "state_update": {"next_objective": "retrieve_options"},
                    },
                ),
            ),
            (
                (
                    "b2_c04_offer",
                    "search_products",
                    {
                        "query": "air fryer",
                        "max_budget": 45,
                        "budget_currency": "USD",
                    },
                ),
                (
                    "b2_c04_offer_repeat",
                    "search_products",
                    {
                        "query": "air fryer",
                        "max_budget": 45,
                        "budget_currency": "USD",
                    },
                ),
            ),
            (
                (
                    "b2_c04_state",
                    "propose_commercial_state_update",
                    {
                        "response_text": (
                            "Na budget ya 45 dollars, option moins chère ezali te na "
                            "offre actuelle."
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
        )
        if outcome_probe:
            response_tool_rounds = (
                (
                    ("b2_denied", "unregistered_capability", {"secret": "omit"}),
                    (
                        "b2_failed",
                        "get_product_details",
                        {"sellable_item_id": str(uuid.uuid4())},
                    ),
                ),
                *response_tool_rounds[1:],
            )

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["Authorization"] == f"Bearer {credential}"
            payloads.append(json.loads(request.content))
            index = len(payloads) - 1
            if failure_index == index and not missing_usage:
                return httpx.Response(200, json={"malformed": True})
            tool_calls = response_tool_rounds[index]
            response = {
                "id": f"chatcmpl_cli_{index}",
                "object": "chat.completion",
                "model": "deepseek-v4-flash",
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "reasoning_content": (
                                "CLI_HIDDEN_REASONING_MUST_NOT_PERSIST"
                            ),
                            "tool_calls": [
                                {
                                    "id": call_id,
                                    "type": "function",
                                    "function": {
                                        "name": tool_name,
                                        "arguments": json.dumps(arguments),
                                    },
                                }
                                for call_id, tool_name, arguments in tool_calls
                            ],
                        },
                        "finish_reason": "tool_calls",
                    }
                ],
                "usage": {
                    "prompt_tokens": 80 + index,
                    "completion_tokens": 20,
                    "total_tokens": 100 + index,
                    "prompt_cache_hit_tokens": 10,
                    "prompt_cache_miss_tokens": 70 + index,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            }
            if failure_index == index and missing_usage:
                response.pop("usage")
            if under_reservation_index == index:
                response["usage"] = {
                    "prompt_tokens": 39_000,
                    "completion_tokens": 100,
                    "total_tokens": 39_100,
                    "prompt_cache_hit_tokens": 1_000,
                    "prompt_cache_miss_tokens": 38_000,
                    "completion_tokens_details": {"reasoning_tokens": 50},
                }
            return httpx.Response(200, json=response)

        return _DeepSeekHTTPTransport(
            api_key=credential,
            timeout_s=12,
            http_transport=httpx.MockTransport(handler),
        )

    return build


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

    hidden_reasoning = "AI5B2_ALL_CASES_MOCKED_HIDDEN_REASONING"
    response_tools = (
        ("b2_c01_offer", "search_products", {"query": "air fryer"}),
        (
            "b2_c01_state",
            "propose_commercial_state_update",
            {
                "response_text": (
                    "Le MBB Test Air Fryer 6L coûte 55 USD, est disponible et "
                    "vendable maintenant."
                ),
                "state_update": {
                    "selected_sellable_item_ids": [str(truth.available_item_id)],
                    "purchase_intent": "considering",
                    "next_objective": "clarify_choice",
                },
            },
        ),
        (
            "b2_c02_handoff",
            "request_human_handoff",
            {
                "reason_category": "qualified_purchase_intent",
                "selected_sellable_item_id": str(truth.available_item_id),
                "purchase_intent": "ready",
            },
        ),
        (
            "b2_c03_offer",
            "get_product_details",
            {"sellable_item_id": str(truth.unavailable_item_id)},
        ),
        (
            "b2_c03_state",
            "propose_commercial_state_update",
            {
                "response_text": (
                    "Le modèle 8L est en rupture de stock; je ne peux pas le "
                    "déclarer disponible."
                ),
                "state_update": {"next_objective": "retrieve_options"},
            },
        ),
        (
            "b2_c04_offer",
            "search_products",
            {"query": "air fryer", "max_budget": 45, "budget_currency": "USD"},
        ),
        (
            "b2_c04_state",
            "propose_commercial_state_update",
            {
                "response_text": (
                    "Na budget ya 45 dollars, option moins chère ezali te na offre "
                    "actuelle."
                ),
                "state_update": {
                    "current_goal": "Trouver un air fryer moins cher",
                    "decision_constraints": [{"kind": "budget", "value": "45 USD"}],
                    "purchase_intent": "considering",
                    "next_objective": "retrieve_options",
                },
            },
        ),
    )
    payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.deepseek.com/chat/completions"
        assert request.headers["Authorization"] == "Bearer inert-test-credential"
        payload = json.loads(request.content)
        payloads.append(payload)
        index = len(payloads) - 1
        call_id, tool_name, arguments = response_tools[index]
        if index in {1, 4, 6}:
            assert any(item.get("role") == "tool" for item in payload["messages"])
        if tool_name in {
            "search_products",
            "request_human_handoff",
            "get_product_details",
        }:
            validated_tools.append(tool_name)
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl_b2_all_{index}",
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
                    "prompt_tokens": 80 + index + 1,
                    "completion_tokens": 20,
                    "total_tokens": 100 + index + 1,
                    "prompt_cache_hit_tokens": 10,
                    "prompt_cache_miss_tokens": 70 + index + 1,
                    "completion_tokens_details": {"reasoning_tokens": 5},
                },
            },
        )

    profile = AI5B2BudgetProfile()
    pricing = CanaryPricingVerificationRecord(
        record_id="synthetic:pricing:ai5b2-postgres",
        source="synthetic://offline-fixture-not-official-pricing",
        verified_at="synthetic-not-a-real-verification-time",
        input_usd_per_million=Decimal("0.50"),
        output_usd_per_million=Decimal("1.00"),
        synthetic=True,
    )
    ledger = OfflineBudgetLedger(profile.offline_limits())
    holder: dict[str, object] = {}
    credential_loads = 0

    def credential_loader() -> str:
        nonlocal credential_loads
        credential_loads += 1
        return "inert-test-credential"

    def live_factory(credential: str):
        assert credential == "inert-test-credential"
        transport = _DeepSeekHTTPTransport(
            api_key=credential,
            timeout_s=12,
            http_transport=httpx.MockTransport(handler),
        )
        deepseek = DeepSeekAdapter(api_key=credential, transport=transport)
        budgeted = CumulativeBudgetProvider(
            deepseek,
            ledger=ledger,
            profile=profile,
            pricing=pricing,
            request_reservation=_deepseek_reservation(deepseek),
        )
        controller = EvaluationDeadlineAdapter(
            budgeted,
            clock=SystemEvaluationClock(),
            deadline_seconds=12,
        )
        holder.update(budgeted=budgeted, controller=controller)
        return controller

    async def run_stage(provider) -> None:
        await _install_closed_runtime(
            monkeypatch,
            factory,
            provider,
            allow_mocked_provider_http=True,
        )
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
        calls_before_replay = len(payloads)
        replay, _, _ = await _run_m1(
            phone="+243810006202",
            content=AI5B2_CANARIES[1].customer_message,
            message_id=duplicate_id,
            whatsapp_message_id=duplicate_wa_id,
            timestamp=started + timedelta(minutes=1),
        )
        assert first_handoff["status"] == "waiting_for_human"
        assert replay["status"] == "duplicate_ignored"
        assert len(payloads) == calls_before_replay
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

    run_id = "synthetic-ai5b2-postgres-run"
    baseline_commit = "1" * 40
    authorization_record_id = "synthetic:authorization:ai5b2-postgres"
    await dispatch_guarded_canary_stage(
        AI5B2ProviderSelection(
            mode=CanaryProviderMode.offline_mocked_http,
            explicit_live_opt_in=True,
            run_id=run_id,
            current_baseline_commit=baseline_commit,
            authorization=CanaryAuthorizationRecord(
                record_id=authorization_record_id,
                run_id=run_id,
                baseline_commit=baseline_commit,
                synthetic=True,
            ),
            pricing_verification=pricing,
            reviewer_assignment=CanaryReviewerAssignmentRecord(
                record_id="synthetic:reviewer-assignment:ai5b2-postgres",
                reviewer_id="synthetic:reviewer:not-a-human-review",
                drc_language_familiarity_confirmed=True,
                synthetic=True,
            ),
            budget_decision=_budget_decision_record(
                run_id,
                baseline_commit,
                authorization_record_id,
            ),
            external_effects_disabled=True,
            disposable_database_isolated=True,
        ),
        offline_factory=lambda: pytest.fail(
            "offline factory bypassed guarded dispatch"
        ),
        credential_loader=credential_loader,
        live_factory=live_factory,
        stage_runner=run_stage,
    )
    budgeted = holder["budgeted"]
    controller = holder["controller"]
    assert isinstance(budgeted, CumulativeBudgetProvider)
    assert isinstance(controller, EvaluationDeadlineAdapter)
    assert credential_loads == 1
    assert terminal_refreshes.count(truth.available_item_id) == 1
    assert terminal_refreshes.count(truth.unavailable_item_id) == 1
    assert len(payloads) == 7

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
                    item.finish_reason for item in budgeted.results[start:end]
                ),
            )
        )

    report = CanaryBridgeEvidence(
        evidence_label=CanaryProviderMode.offline_mocked_http,
        provider="deepseek",
        model="deepseek-v4-flash",
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
    assert '"evidence_label":"offline_mocked_http"' in serialized
    assert len(payloads) == 7 and ledger.provider_calls == 7
    assert ledger.reserved_tokens == sum(
        call.reserved_tokens for call in budgeted.call_evidence
    )
    assert ledger.unresolved_reserved_tokens == 0
    assert ledger.observed_tokens == sum(100 + index for index in range(1, 8))
    assert ledger.committed_tokens == ledger.observed_tokens
    assert report.real_provider_network_calls == 0
    assert report.actual_provider_api_tokens == 0
    assert report.actual_provider_cost_usd == 0
    assert hidden_reasoning not in serialized
    assert controller.unfinished_task_count == 0
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
                    "Le MBB Test Air Fryer 6L coûte 55 USD et est disponible "
                    "maintenant."
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
    budgeted = CumulativeBudgetProvider(
        deepseek,
        ledger=ledger,
        profile=profile,
        request_reservation=_deepseek_reservation(deepseek),
    )
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
    assert budgeted.call_evidence[0].reserved_input_tokens == (
        conservative_json_request_reservation(
            payloads[0], max_output_tokens=512
        ).input_tokens
    )
    assert budgeted.call_evidence[1].reserved_input_tokens == (
        conservative_json_request_reservation(
            payloads[1], max_output_tokens=512
        ).input_tokens
    )
    assert (
        budgeted.call_evidence[1].reserved_input_tokens
        > budgeted.call_evidence[0].reserved_input_tokens
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


def test_actual_cli_orchestrates_complete_mocked_stage_and_cleanup(
    tmp_path, capsys
) -> None:
    run_id = "synthetic-cli-success-ai5b2"
    payloads: list[dict] = []
    credential_loads = 0

    def credential_loader() -> str:
        nonlocal credential_loads
        credential_loads += 1
        partial_before_credential = json.loads(
            (tmp_path / run_id / "partial.json").read_text(encoding="utf-8")
        )
        assert partial_before_credential["budget_decision_metadata"]["accepted"] is True
        assert (
            partial_before_credential["budget_decision_metadata"]["accepted_by"]
            == "project-owner"
        )
        return "inert-cli-test-credential"

    result = bridge_main(
        _cli_arguments(tmp_path, run_id),
        _test_overrides=CanaryCLIOverrides(
            credential_loader=credential_loader,
            transport_builder=_mocked_cli_transport(payloads),
        ),
    )
    capsys.readouterr()
    evidence_path = tmp_path / run_id / "evidence.json"
    partial_path = tmp_path / run_id / "partial.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert result == 0 and credential_loads == 1
    assert len(payloads) == 7
    assert evidence["overall_decision"] == "manual_review_pending"
    assert evidence["evidence_state"] == "final"
    assert evidence["budget_decision_metadata"]["accepted"] is True
    assert evidence["budget_decision_metadata"]["accepted_by"] == "project-owner"
    assert evidence["budget_decision_metadata"]["run_id"] == run_id
    assert evidence["budget_decision_metadata"]["baseline_commit"]
    assert evidence["external_effect_guards"] == {
        "AI_ADAPTER": "disabled",
        "AI_TURN_PROVIDER": "deepseek",
        "WHATSAPP_SEND_ENABLED": False,
        "CRM_SEND_ENABLED": False,
        "PAYMENT_SEND_ENABLED": False,
        "RELANCE_ENABLED": False,
        "SCHEDULED_TASKS_ENABLED": False,
        "M1_MAPS_FANOUT_ENABLED": False,
        "external_business_effects_verified_disabled": True,
    }
    assert [case["deterministic_status"] for case in evidence["cases"]] == [
        "passed",
        "passed",
        "passed",
        "passed",
    ]
    assert evidence["replay_evidence"] == {
        "status": "duplicate_ignored",
        "provider_requests_added": 0,
        "messages_added": 0,
        "audits_added": 0,
        "tickets_added": 0,
    }
    assert evidence["protected_snapshots"]["matched"] is True
    assert evidence["reserved_provider_calls"] == 7
    assert evidence["unresolved_reserved_tokens"] == 0
    assert evidence["settled_actual_tokens"] == sum(range(100, 107))
    assert evidence["budget_committed_tokens"] == sum(range(100, 107))
    assert evidence["reservation_violations"] == 0
    assert evidence["reserved_durable_actions"] == 1
    assert evidence["limits"]["durable_actions"] == 1
    assert evidence["observed_total_tokens"] == sum(range(100, 107))
    assert evidence["real_provider_network_calls"] == 0
    assert evidence["actual_provider_api_tokens"] == 0
    assert evidence["actual_provider_cost_usd"] == "0"
    assert "55 USD" in evidence["cases"][0]["transcript"][1]["content"]
    assert evidence["cases"][0]["commercial_evaluation"]["status"] == "passed"
    assert all(
        call["reservation_method"] == "utf8_wire_bytes_plus_json_nodes_estimate_v1"
        for call in evidence["provider_calls"]
    )
    assert evidence["tool_trace_complete"] is True
    traces = evidence["tool_traces"]
    assert [item["sequence"] for item in traces] == list(range(1, len(traces) + 1))
    c01_searches = [
        item
        for item in traces
        if item["case_id"] == "B2-C01-FR-FRESH-P6"
        and item["capability_name"] == "search_products"
    ]
    assert [item["search_relationship"] for item in c01_searches] == [
        "first_search",
        "refined_search",
    ]
    assert c01_searches[0]["provider_request_index"] == 1
    assert c01_searches[1]["provider_request_index"] == 1
    assert c01_searches[0]["tool_call_id"] != c01_searches[1]["tool_call_id"]
    assert c01_searches[0]["validated_arguments"]["max_budget"] is None
    assert c01_searches[1]["validated_arguments"]["max_budget"] == "55"
    assert (
        c01_searches[0]["authoritative_result"]["items"][0]["current_usd_price"]
        == "55.00"
    )
    assert (
        c01_searches[0]["authoritative_result"]["items"][0]["availability"]
        == "available"
    )
    c04_searches = [
        item
        for item in traces
        if item["case_id"] == "B2-C04-SW-FR-BUDGET"
        and item["capability_name"] == "search_products"
    ]
    assert [item["search_relationship"] for item in c04_searches] == [
        "first_search",
        "repeated_identical_search",
    ]
    refresh = next(item for item in traces if item["event_type"] == "terminal_refresh")
    handoff = next(
        item for item in traces if item["capability_name"] == "request_human_handoff"
    )
    assert refresh["tool_call_id"] == handoff["tool_call_id"]
    assert refresh["authoritative_result"]["read_at"]
    assert refresh["authoritative_result"]["inventory_updated_at"]
    assert refresh["authoritative_result"]["price_effective_at"]
    assert refresh["authoritative_result"]["is_sellable_now"] is True
    after_terminal = next(
        item for item in traces if item["tool_call_id"] == "b2_c02_after_terminal"
    )
    assert after_terminal["outcome"] == "not_executed"
    assert after_terminal["safe_error_category"] is None
    assert after_terminal["safe_error_code"] == "terminal_capability_succeeded"
    assert after_terminal["validated_arguments"] is None
    replay_trace = next(
        item for item in traces if item["event_type"] == "replay_suppression"
    )
    assert replay_trace["outcome"] == "suppressed"
    assert replay_trace["authoritative_result"]["provider_requests_added"] == 0
    assert evidence["cases"][1]["persistence"]["ticket_count"] == 1
    assert evidence["cases"][1]["persistence"]["audit_transitions"]
    assert "rupture" in evidence["cases"][2]["transcript"][1]["content"]
    assert (
        evidence["cases"][3]["persistence"]["commercial_state"]["decision_constraints"][
            0
        ]["value"]
        == "45 USD"
    )
    assert all(call["latency_ms"] is not None for call in evidence["provider_calls"])
    assert all(case["manual_review_status"] == "pending" for case in evidence["cases"])
    assert evidence["cleanup"]["database_dropped"] is True
    assert evidence["cleanup"]["cluster_stopped"] is True
    assert evidence["cleanup"]["temporary_directory_removed"] is True
    assert evidence["cleanup"]["run_scoped_settings_restored"] is True
    _assert_cleanup_observed(evidence["cleanup"])
    assert partial_path.is_file()
    partial = json.loads(partial_path.read_text(encoding="utf-8"))
    assert partial["budget_decision_metadata"] == evidence["budget_decision_metadata"]
    assert "CLI_HIDDEN_REASONING_MUST_NOT_PERSIST" not in evidence_path.read_text(
        encoding="utf-8"
    )
    assert '"secret":"omit"' not in evidence_path.read_text(encoding="utf-8")


def test_actual_cli_distinguishes_denied_and_failed_capabilities(
    tmp_path, capsys
) -> None:
    run_id = "synthetic-cli-tool-outcomes-ai5b2"
    payloads: list[dict] = []
    result = bridge_main(
        _cli_arguments(tmp_path, run_id),
        _test_overrides=CanaryCLIOverrides(
            credential_loader=lambda: "inert-cli-test-credential",
            transport_builder=_mocked_cli_transport(payloads, outcome_probe=True),
        ),
    )
    capsys.readouterr()
    evidence = json.loads(
        (tmp_path / run_id / "evidence.json").read_text(encoding="utf-8")
    )

    assert result == 1 and len(payloads) == 2
    outcomes = {
        item["tool_call_id"]: item
        for item in evidence["tool_traces"]
        if item["event_type"] == "capability_execution"
    }
    assert outcomes["b2_denied"]["outcome"] == "denied"
    assert outcomes["b2_denied"]["safe_error_category"] == "unknown_tool"
    assert outcomes["b2_denied"]["validated_arguments"] is None
    assert outcomes["b2_failed"]["outcome"] == "failed"
    assert outcomes["b2_failed"]["safe_error_category"] == "execution_failed"
    assert outcomes["b2_failed"]["safe_error_code"] == "sellable_item_not_found"
    assert outcomes["b2_failed"]["validated_arguments"]["sellable_item_id"]
    serialized = json.dumps(evidence)
    assert "omit" not in serialized
    assert evidence["tool_trace_complete"] is True
    assert evidence["cleanup"]["database_dropped"] is True
    _assert_cleanup_observed(evidence["cleanup"])


def test_actual_cli_trace_persistence_failure_stops_dispatch_and_survives_cleanup(
    tmp_path, capsys
) -> None:
    run_id = "synthetic-cli-trace-failure-ai5b2"
    payloads: list[dict] = []

    def fail_trace_persistence(_records, _complete) -> None:
        raise OSError("synthetic trace persistence failure")

    result = bridge_main(
        _cli_arguments(tmp_path, run_id),
        _test_overrides=CanaryCLIOverrides(
            credential_loader=lambda: "inert-cli-test-credential",
            transport_builder=_mocked_cli_transport(payloads),
            tool_trace_persist_hook=fail_trace_persistence,
        ),
    )
    capsys.readouterr()
    evidence_path = tmp_path / run_id / "evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert result == 1 and len(payloads) == 1
    assert evidence["stop_reason"] == "tool_trace_persistence_failed"
    assert evidence["failed_request_index"] == 1
    assert evidence["tool_trace_complete"] is False
    assert evidence["tool_trace_persistence_failed"] is True
    assert evidence["evidence_state"] == "final"
    assert evidence["database_lifecycle"]["database_dropped"] is True
    assert evidence["database_lifecycle"]["temporary_directory_removed"] is True
    _assert_cleanup_observed(evidence["database_lifecycle"])


@pytest.mark.parametrize(
    ("failure_index", "missing_usage", "expected_calls", "expected_failed_case"),
    (
        (1, True, 2, "B2-C01-FR-FRESH-P6"),
        (3, False, 4, "B2-C03-FR-INJECTION-P8"),
    ),
)
def test_actual_cli_latches_failure_and_persists_partial_evidence(
    tmp_path,
    capsys,
    failure_index,
    missing_usage,
    expected_calls,
    expected_failed_case,
) -> None:
    run_id = f"synthetic-cli-failure-{failure_index}-ai5b2"
    payloads: list[dict] = []
    result = bridge_main(
        _cli_arguments(tmp_path, run_id),
        _test_overrides=CanaryCLIOverrides(
            credential_loader=lambda: "inert-cli-test-credential",
            transport_builder=_mocked_cli_transport(
                payloads,
                failure_index=failure_index,
                missing_usage=missing_usage,
            ),
        ),
    )
    capsys.readouterr()
    evidence_path = tmp_path / run_id / "evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert result == 1
    assert len(payloads) == expected_calls
    assert evidence["overall_decision"] == "failed"
    assert evidence["failed_case_id"] == expected_failed_case
    assert evidence["failed_request_index"] == expected_calls
    assert evidence["provider_calls"][-1]["total_tokens"] is None
    assert evidence["provider_calls"][-1]["estimated_cost_usd"] is None
    assert evidence["actual_provider_api_tokens"] is None
    assert evidence["actual_provider_cost_usd"] is None
    assert evidence["reserved_provider_calls"] == expected_calls
    dispatched_calls = [
        call for call in evidence["provider_calls"] if call["transport_dispatched"]
    ]
    assert evidence["reserved_tokens"] == sum(
        call["reserved_tokens"] for call in dispatched_calls
    )
    assert (
        evidence["unresolved_reserved_tokens"]
        == dispatched_calls[-1]["reserved_tokens"]
    )
    assert evidence["skipped_case_ids"]
    assert evidence["cleanup"]["database_dropped"] is True
    assert evidence["cleanup"]["cluster_stopped"] is True
    assert evidence["cleanup"]["temporary_directory_removed"] is True
    _assert_cleanup_observed(evidence["cleanup"])
    assert (tmp_path / run_id / "partial.json").is_file()


def test_actual_cli_latches_under_reservation_before_later_cases(
    tmp_path, capsys
) -> None:
    run_id = "synthetic-cli-under-reservation-ai5b2"
    payloads: list[dict] = []
    result = bridge_main(
        _cli_arguments(tmp_path, run_id),
        _test_overrides=CanaryCLIOverrides(
            credential_loader=lambda: "inert-cli-test-credential",
            transport_builder=_mocked_cli_transport(
                payloads,
                under_reservation_index=0,
            ),
        ),
    )
    capsys.readouterr()
    evidence = json.loads(
        (tmp_path / run_id / "evidence.json").read_text(encoding="utf-8")
    )

    assert result == 1
    assert len(payloads) == 1
    assert evidence["stop_reason"] == "budget_reservation_violation"
    assert evidence["failed_request_index"] == 1
    assert evidence["reservation_violations"] == 1
    assert evidence["unresolved_reserved_tokens"] == 0
    assert evidence["settled_actual_tokens"] == 39_100
    assert evidence["provider_calls"][0]["reservation_settled"] is True
    assert evidence["provider_calls"][0]["reservation_violation"] is True
    assert evidence["provider_calls"][0]["total_tokens"] == 39_100
    assert evidence["skipped_case_ids"] == [
        "B2-C02-FR-QUALIFIED",
        "B2-C03-FR-INJECTION-P8",
        "B2-C04-SW-FR-BUDGET",
    ]
    assert evidence["cleanup"]["database_dropped"] is True
    assert evidence["cleanup"]["cluster_stopped"] is True
    assert evidence["cleanup"]["temporary_directory_removed"] is True
    _assert_cleanup_observed(evidence["cleanup"])


@pytest.mark.parametrize("failure", (RuntimeError("setup failed"), KeyboardInterrupt()))
def test_actual_cli_setup_failure_and_cancellation_cleanup(
    tmp_path, capsys, failure
) -> None:
    run_id = f"synthetic-cli-cleanup-{type(failure).__name__.lower()}"
    credential_loads = 0

    def credential_loader() -> str:
        nonlocal credential_loads
        credential_loads += 1
        return "must-not-load"

    def stop_after_prepare(_runtime) -> None:
        raise failure

    result = bridge_main(
        _cli_arguments(tmp_path, run_id),
        _test_overrides=CanaryCLIOverrides(
            credential_loader=credential_loader,
            transport_builder=_mocked_cli_transport([]),
            after_prepare=stop_after_prepare,
        ),
    )
    capsys.readouterr()
    evidence = json.loads(
        (tmp_path / run_id / "evidence.json").read_text(encoding="utf-8")
    )

    assert result == 1 and credential_loads == 0
    assert evidence["overall_decision"] == "failed"
    assert evidence["database_lifecycle"]["database_dropped"] is True
    assert evidence["database_lifecycle"]["cluster_stopped"] is True
    assert evidence["database_lifecycle"]["temporary_directory_removed"] is True
    _assert_cleanup_observed(evidence["database_lifecycle"])
