"""AI-5B1 offline certification against one runner-owned PostgreSQL cluster."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.adapters.ai.deepseek_adapter import DeepSeekAdapter
from app.ai.commercial_state import CommercialState, read_commercial_state
from app.ai.offline_certification import (
    AI5B1_OUTER_WATCHDOG_SECONDS,
    AI5B1_PROVIDER_DEADLINE_SECONDS,
    AI5B1_SAFE_BOUNDARY_SECONDS,
    OfflineBudgetExceeded,
    OfflineBudgetLedger,
    OfflineBudgetLimits,
    OfflineLatencyClass,
    RecordingScriptedProvider,
    ScriptedProviderStep,
    classify_latency,
    normalized_timeout,
    redacted_evidence_json,
)
from app.ai.provider_contract import (
    ProviderErrorCategory,
    ProviderFinishReason,
    ProviderMessage,
    ProviderReasoningProfile,
    ProviderToolCall,
    ProviderTurnError,
    ProviderTurnRequest,
    ProviderTurnResult,
    ProviderUsage,
)
from app.ai.turn import AITurnLimits
from app.config import get_settings
from app.i18n.messages import t
from app.models.conversation import Conversation
from app.models.message import Message
from app.modules.m4_conversation.ownership import transition_ownership

from test_ai_offline_integration_postgres import (
    TRUNCATE,
    _RuntimeEvidence,
    _Task,
    _continuation,
    _install_runtime,
    _seed_business_truth,
    _stored_state,
    _tool_result,
)


DATABASE_URL = os.environ.get("AI5B1_TEST_DATABASE_URL")
CLUSTER_ID = os.environ.get("AI5B1_DISPOSABLE_CLUSTER_ID")
CLUSTER_PREFIX = "mbb-ai5b1-cluster-"
DATABASE_PREFIX = "ai5b1_cert_"
HIDDEN_REASONING_SENTINEL = "AI5B1_HIDDEN_REASONING_MUST_NOT_SURVIVE"
LATE_RESULT_SENTINEL = "AI5B1_LATE_RESULT_MUST_NOT_PERSIST"
IDEMPOTENCY_SECRET = "ai5b1-synthetic-idempotency-secret-" + ("x" * 32)
PROTECTED_TABLES = (
    "products",
    "sellable_items",
    "product_media",
    "sellable_item_prices",
    "exchange_rates",
    "inventory_statuses",
    "orders",
    "payments",
)

pytestmark = pytest.mark.skipif(
    not DATABASE_URL or not CLUSTER_ID,
    reason="run through scripts/run_ai5b1_offline_certification.py",
)


def _assert_disposable_identity() -> None:
    assert DATABASE_URL is not None
    assert CLUSTER_ID is not None and CLUSTER_ID.startswith(CLUSTER_PREFIX)
    parsed = make_url(DATABASE_URL)
    assert parsed.drivername == "postgresql+asyncpg"
    assert parsed.host == "127.0.0.1"
    assert parsed.port is not None and parsed.port != 5432
    assert parsed.database is not None and parsed.database.startswith(DATABASE_PREFIX)
    assert parsed.username == "ai5b1_admin"
    assert parsed.password in {None, ""}
    assert os.environ.get("AI3F_TEST_DATABASE_URL") is None


async def _protected_snapshot(factory: async_sessionmaker) -> dict[str, str]:
    snapshot: dict[str, str] = {}
    async with factory() as session:
        for table_name in PROTECTED_TABLES:
            rows = await session.scalar(
                text(
                    "SELECT COALESCE(jsonb_agg(to_jsonb(row_data) "
                    "ORDER BY to_jsonb(row_data)::text), '[]'::jsonb) "
                    f"FROM mbb.{table_name} AS row_data"
                )
            )
            snapshot[table_name] = json.dumps(rows, default=str, sort_keys=True)
    return snapshot


@pytest_asyncio.fixture(loop_scope="function")
async def postgres():
    _assert_disposable_identity()
    assert DATABASE_URL is not None
    engine = create_async_engine(DATABASE_URL, pool_size=8)
    async with engine.begin() as connection:
        await connection.execute(TRUNCATE)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    truth = await _seed_business_truth(factory)
    baseline = await _protected_snapshot(factory)
    try:
        yield factory, truth, baseline
    finally:
        async with engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await engine.dispose()


async def _install_closed_runtime(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker,
    adapter: RecordingScriptedProvider,
) -> _RuntimeEvidence:
    import app.adapters as adapters
    from app.adapters.ai import deepseek_adapter

    evidence = await _install_runtime(monkeypatch, factory, adapter)
    monkeypatch.setattr(
        adapters,
        "get_crm_adapter",
        lambda: pytest.fail("CRM adapter resolution was attempted"),
    )
    monkeypatch.setattr(
        adapters,
        "get_payment_adapter",
        lambda: pytest.fail("payment adapter resolution was attempted"),
    )

    async def reject_http(*_args, **_kwargs):
        pytest.fail("live provider HTTP transport was attempted")

    monkeypatch.setattr(
        deepseek_adapter._DeepSeekHTTPTransport,
        "create_chat_completion",
        reject_http,
    )
    return evidence


async def _run_m1(
    *,
    phone: str,
    content: str,
    message_id: uuid.UUID | None = None,
    whatsapp_message_id: str | None = None,
    timestamp: datetime | None = None,
) -> tuple[dict, uuid.UUID, str]:
    from app.tasks import m1

    source_message_id = message_id or uuid.uuid4()
    wa_id = whatsapp_message_id or f"ai5b1-{source_message_id}"
    at = timestamp or datetime(2026, 9, 1, 8, tzinfo=timezone.utc)
    result = await m1._process(
        task=_Task(),
        message_id=str(source_message_id),
        customer_phone=phone,
        content=content,
        content_type="text",
        timestamp=at.isoformat(),
        whatsapp_message_id=wa_id,
    )
    return result, source_message_id, wa_id


async def _assert_protected_unchanged(
    factory: async_sessionmaker,
    baseline: dict[str, str],
) -> None:
    assert await _protected_snapshot(factory) == baseline


def _finalizer(
    call_id: str,
    response_text: str,
    *,
    selected_item_id: uuid.UUID | None = None,
) -> ProviderTurnResult:
    state_update: dict[str, object] = {
        "current_goal": "Choisir un air fryer familial",
        "expressed_needs": ["famille de quatre personnes"],
        "decision_constraints": [{"kind": "budget", "value": "60 USD"}],
        "purchase_intent": "considering",
        "next_objective": "clarify_choice",
    }
    if selected_item_id is not None:
        state_update["selected_sellable_item_ids"] = [str(selected_item_id)]
    return ProviderTurnResult(
        tool_calls=(
            ProviderToolCall(
                call_id=call_id,
                capability_name="propose_commercial_state_update",
                arguments={
                    "response_text": response_text,
                    "state_update": state_update,
                },
            ),
        ),
        finish_reason=ProviderFinishReason.tool_call,
    )


@pytest.mark.asyncio
async def test_b1_o01_normal_freshness(postgres, monkeypatch) -> None:
    factory, truth, baseline = postgres
    observed_offer: dict[str, object] = {}
    first_input = "Je cherche un air fryer pour quatre personnes."
    second_input = "Mon budget actuel est 60 dollars."

    def clarify(_request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(
            text="Quel est ton budget actuel ?",
            finish_reason=ProviderFinishReason.completed,
        )

    def current_offer(request: ProviderTurnRequest) -> ProviderTurnResult:
        assert first_input in request.messages[0].content
        assert second_input in request.messages[0].content
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="b1_o01_offer",
                    capability_name="search_products",
                    arguments={
                        "query": "air fryer",
                        "max_budget": 60,
                        "budget_currency": "USD",
                    },
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("b1_o01_offer"),
        )

    def ground_and_finalize(request: ProviderTurnRequest) -> ProviderTurnResult:
        tool_result = _tool_result(request, "b1_o01_offer")
        assert tool_result.status == "success" and tool_result.output is not None
        items = tool_result.output["items"]
        assert isinstance(items, list) and len(items) == 1
        observed_offer.update(items[0])
        response = (
            f"{observed_offer['name']} {observed_offer['model_label']} coûte "
            f"{observed_offer['current_usd_price']} USD et est disponible."
        )
        return _finalizer(
            "b1_o01_state",
            response,
            selected_item_id=truth.available_item_id,
        )

    adapter = RecordingScriptedProvider(
        (
            ScriptedProviderStep(clarify, represented_latency_ms=4_000),
            ScriptedProviderStep(current_offer, represented_latency_ms=4_500),
            ScriptedProviderStep(ground_and_finalize, represented_latency_ms=1_000),
        )
    )
    evidence = await _install_closed_runtime(monkeypatch, factory, adapter)
    first, _, _ = await _run_m1(phone="+243810005101", content=first_input)
    second, source_message_id, _ = await _run_m1(
        phone="+243810005101",
        content=second_input,
        timestamp=datetime(2026, 9, 1, 8, 1, tzinfo=timezone.utc),
    )

    conversation, messages, audits, tickets = await _stored_state(
        factory, "+243810005101"
    )
    state = await read_commercial_state_for_test(factory, conversation.conversation_id)
    assert first["status"] == "processed" and second["status"] == "processed"
    assert len(adapter.requests) == 3 and adapter.network_calls == 0
    assert observed_offer["sellable_item_id"] == str(truth.available_item_id)
    assert observed_offer["offer_status"] == "sellable_now"
    assert observed_offer["availability"] == "available"
    assert Decimal(str(observed_offer["current_usd_price"])) == Decimal("55.00")
    assert sum(item.direction == "inbound" for item in messages) == 2
    assert sum(item.direction == "outbound" for item in messages) == 2
    assert len(audits) == 2 and audits[-1].source_message_id == source_message_id
    assert audits[-1].commercial_state_revision_before == 0
    assert audits[-1].commercial_state_revision_after == 1
    assert state is not None and state.revision == 1
    assert state.selected_sellable_item_ids == [truth.available_item_id]
    assert tickets == []
    assert evidence.send_boundaries == [
        item.message_id for item in messages if item.direction == "outbound"
    ]
    await _assert_protected_unchanged(factory, baseline)


async def read_commercial_state_for_test(
    factory: async_sessionmaker,
    conversation_id: uuid.UUID,
) -> CommercialState | None:
    async with factory() as session:
        return await read_commercial_state(session, conversation_id)


@pytest.mark.asyncio
async def test_b1_o02_terminal_idempotency(postgres, monkeypatch) -> None:
    factory, truth, baseline = postgres
    import app.modules.product_offer.service as offer_service

    refreshes: list[uuid.UUID] = []
    original_require_offer = offer_service.require_product_offer

    async def record_refresh(session, sellable_item_id):
        refreshes.append(sellable_item_id)
        return await original_require_offer(session, sellable_item_id)

    monkeypatch.setattr(offer_service, "require_product_offer", record_refresh)

    def qualified(_request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="b1_o02_handoff",
                    capability_name="request_human_handoff",
                    arguments={
                        "reason_category": "qualified_purchase_intent",
                        "selected_sellable_item_id": str(truth.available_item_id),
                        "purchase_intent": "ready",
                    },
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("b1_o02_handoff"),
        )

    adapter = RecordingScriptedProvider((ScriptedProviderStep(qualified),))
    await _install_closed_runtime(monkeypatch, factory, adapter)
    message_id = uuid.uuid4()
    wa_id = f"ai5b1-terminal-{message_id}"
    content = "Je prends le modèle 6L à 55 dollars."
    first, source_message_id, _ = await _run_m1(
        phone="+243810005102",
        content=content,
        message_id=message_id,
        whatsapp_message_id=wa_id,
    )
    replay, _, _ = await _run_m1(
        phone="+243810005102",
        content=content,
        message_id=message_id,
        whatsapp_message_id=wa_id,
    )
    distinct, _, _ = await _run_m1(
        phone="+243810005102",
        content=content,
        timestamp=datetime(2026, 9, 1, 8, 2, tzinfo=timezone.utc),
    )

    conversation, messages, audits, tickets = await _stored_state(
        factory, "+243810005102"
    )
    assert first["status"] == "waiting_for_human"
    assert replay["status"] == "duplicate_ignored"
    assert distinct["status"] == "waiting_for_human"
    assert len(adapter.requests) == 1 and refreshes == [truth.available_item_id]
    assert sum(item.direction == "inbound" for item in messages) == 2
    assert sum(item.direction == "outbound" for item in messages) == 1
    assert len(audits) == 1 and audits[0].source_message_id == source_message_id
    assert audits[0].outcome == "handoff_requested"
    assert len(tickets) == 1 and tickets[0].status == "open"
    assert conversation.owner_type == "ai"
    assert conversation.ai_execution_state == "paused"
    assert conversation.ownership_version == 2
    await _assert_protected_unchanged(factory, baseline)


@pytest.mark.asyncio
async def test_b1_o03_stale_suppression(postgres, monkeypatch) -> None:
    factory, truth, baseline = postgres
    import app.modules.m4_conversation.ownership as ownership_module

    original_eligibility = ownership_module.ai_adapter_eligibility

    def scripted_eligibility(configured_name: str):
        if configured_name == "scripted":
            return "eligible"
        return original_eligibility(configured_name)

    monkeypatch.setattr(
        ownership_module,
        "ai_adapter_eligibility",
        scripted_eligibility,
    )
    starts = [asyncio.Event() for _ in range(4)]
    releases = [asyncio.Event() for _ in range(4)]

    def delayed_step(index: int):
        async def delayed(_request: ProviderTurnRequest) -> ProviderTurnResult:
            starts[index].set()
            await releases[index].wait()
            return _finalizer(
                f"b1_o03_state_{index}",
                f"Réponse périmée {index} interdite.",
            )

        return delayed

    adapter = RecordingScriptedProvider(
        tuple(ScriptedProviderStep(delayed_step(index)) for index in range(4))
    )
    await _install_closed_runtime(monkeypatch, factory, adapter)
    phones = [f"+24381000510{index + 3}" for index in range(4)]

    async def run_pending(index: int) -> tuple[asyncio.Task, Conversation]:
        processing = asyncio.create_task(
            _run_m1(phone=phones[index], content=f"Message en attente {index}")
        )
        await asyncio.wait_for(starts[index].wait(), timeout=10)
        async with factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.customer_id == phones[index])
            )
            assert conversation is not None
        return processing, conversation

    processing, conversation = await run_pending(0)
    async with factory() as session:
        session.add(
            Message(
                message_id=uuid.uuid4(),
                conversation_id=conversation.conversation_id,
                timestamp=datetime(2026, 9, 1, 8, 5, tzinfo=timezone.utc),
                direction="inbound",
                content="Inbound plus récent synthétique",
                content_type="text",
                language="french",
                whatsapp_message_id="ai5b1-newer-inbound",
            )
        )
        await session.commit()
    releases[0].set()
    assert (await asyncio.wait_for(processing, timeout=10))[0]["status"] == (
        "persistence_failed"
    )

    processing, conversation = await run_pending(1)
    external_state = CommercialState(
        revision=1,
        current_goal="Révision concurrente autorisée",
        updated_at=datetime(2026, 9, 1, 8, 6, tzinfo=timezone.utc),
    )
    async with factory() as session:
        await session.execute(
            update(Conversation)
            .where(Conversation.conversation_id == conversation.conversation_id)
            .values(
                context={"commercial_state": external_state.model_dump(mode="json")}
            )
        )
        await session.commit()
    releases[1].set()
    assert (await asyncio.wait_for(processing, timeout=10))[0]["status"] == (
        "persistence_failed"
    )

    processing, conversation = await run_pending(2)
    async with factory() as session:
        takeover = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="human",
            expected_version=1,
            actor_account_id=truth.operator_account_id,
            actor_display_name="AI-5B1 Synthetic Operator",
            actor_role="administrator",
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai5b1-version-takeover",
            ai_adapter="scripted",
        )
        returned = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="ai",
            expected_version=takeover.ownership.version,
            actor_account_id=truth.operator_account_id,
            actor_display_name="AI-5B1 Synthetic Operator",
            actor_role="administrator",
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai5b1-version-return",
            ai_adapter="scripted",
        )
        assert returned.ownership.version == 3
    releases[2].set()
    assert (await asyncio.wait_for(processing, timeout=10))[0]["status"] == (
        "persistence_failed"
    )

    processing, conversation = await run_pending(3)
    async with factory() as session:
        takeover = await transition_ownership(
            session,
            conversation_id=conversation.conversation_id,
            target_owner_type="human",
            expected_version=1,
            actor_account_id=truth.operator_account_id,
            actor_display_name="AI-5B1 Synthetic Operator",
            actor_role="administrator",
            idempotency_key=uuid.uuid4(),
            idempotency_secret=IDEMPOTENCY_SECRET,
            request_id="ai5b1-human-takeover",
            ai_adapter="scripted",
        )
        assert takeover.ownership.owner_type == "human"
    releases[3].set()
    assert (await asyncio.wait_for(processing, timeout=10))[0]["status"] == (
        "persistence_failed"
    )

    for index, phone in enumerate(phones):
        conversation, messages, audits, tickets = await _stored_state(factory, phone)
        assert sum(item.direction == "outbound" for item in messages) == 0
        assert audits == [] and tickets == []
        assert all("Réponse périmée" not in item.content for item in messages)
        if index == 1:
            state = await read_commercial_state_for_test(
                factory, conversation.conversation_id
            )
            assert state == external_state
        if index == 2:
            assert conversation.owner_type == "ai"
            assert conversation.ownership_version == 3
        if index == 3:
            assert conversation.owner_type == "human"
            assert conversation.ownership_version == 2
    assert len(adapter.requests) == 4
    await _assert_protected_unchanged(factory, baseline)


@pytest.mark.asyncio
async def test_b1_o04_failure_handoff(postgres, monkeypatch) -> None:
    factory, _truth, baseline = postgres
    missing_item = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")

    def fail_product_read(_request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="b1_o04_missing",
                    capability_name="get_product_details",
                    arguments={"sellable_item_id": str(missing_item)},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("b1_o04_missing"),
        )

    def reliability_handoff(request: ProviderTurnRequest) -> ProviderTurnResult:
        failed = _tool_result(request, "b1_o04_missing")
        assert failed.status == "error" and failed.error is not None
        assert failed.error.safe_code == "sellable_item_not_found"
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="b1_o04_handoff",
                    capability_name="request_human_handoff",
                    arguments={"reason_category": "required_capability_unavailable"},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("b1_o04_handoff"),
        )

    adapter = RecordingScriptedProvider(
        (
            ScriptedProviderStep(fail_product_read),
            ScriptedProviderStep(reliability_handoff),
        )
    )
    await _install_closed_runtime(monkeypatch, factory, adapter)
    result, source_message_id, _ = await _run_m1(
        phone="+243810005107",
        content="Confirme le prix et le stock du produit Z99.",
    )
    conversation, messages, audits, tickets = await _stored_state(
        factory, "+243810005107"
    )
    outbound = [item for item in messages if item.direction == "outbound"]
    assert result["status"] == "waiting_for_human"
    assert len(adapter.requests) == 2 and adapter.network_calls == 0
    assert len(outbound) == 1
    assert outbound[0].content == t("ai4e_handoff_reliability_tool_failure", "french")
    assert "livraison" not in outbound[0].content.lower()
    assert "plus tard" not in outbound[0].content.lower()
    assert len(audits) == 1 and audits[0].source_message_id == source_message_id
    assert audits[0].outcome == "handoff_requested"
    assert len(tickets) == 1
    assert conversation.ai_execution_state == "paused"
    await _assert_protected_unchanged(factory, baseline)


@pytest.mark.asyncio
async def test_b1_o05_telemetry_and_budgets(postgres, monkeypatch) -> None:
    factory, _truth, baseline = postgres
    limits = OfflineBudgetLimits(
        max_provider_calls=1,
        max_total_tokens=100,
        max_reserved_cost_usd=Decimal("0.01"),
        max_durable_actions=1,
        max_output_tokens_per_call=512,
    )
    ledger = OfflineBudgetLedger(limits)

    def measured(_request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(
            text="Mesure synthétique terminée.",
            finish_reason=ProviderFinishReason.max_output,
            usage=ProviderUsage(
                input_tokens=12,
                output_tokens=8,
                total_tokens=20,
                cache_hit_tokens=5,
                cache_miss_tokens=7,
                reasoning_tokens=3,
            ),
        )

    adapter = RecordingScriptedProvider(
        (
            ScriptedProviderStep(
                measured,
                represented_latency_ms=5_500,
                reserved_tokens=100,
                reserved_cost_usd=Decimal("0.005"),
            ),
            ScriptedProviderStep(measured),
        ),
        budget=ledger,
    )
    await _install_closed_runtime(monkeypatch, factory, adapter)
    result, _, _ = await _run_m1(
        phone="+243810005108",
        content="Mesure ce tour synthétique.",
    )
    assert result["status"] == "processed"
    assert len(adapter.evidence) == 1
    call = adapter.evidence[0]
    assert call.latency_class == OfflineLatencyClass.routine_target
    assert call.finish_reason == ProviderFinishReason.max_output
    assert call.input_tokens == 12 and call.output_tokens == 8
    assert call.total_tokens == 20
    assert call.cache_hit_tokens == 5 and call.cache_miss_tokens == 7
    assert call.reasoning_tokens == 3
    assert call.provider_network_calls == 0
    assert call.provider_api_tokens == 0 and call.provider_cost_usd == 0
    assert ledger.provider_calls == 1
    assert ledger.reserved_tokens == 100 and ledger.observed_tokens == 20
    assert ledger.reserved_cost_usd == Decimal("0.005")
    with pytest.raises(OfflineBudgetExceeded, match="provider_calls"):
        await adapter.generate_turn(adapter.requests[0])
    assert len(adapter.requests) == 1
    ledger.reserve_durable_action()
    with pytest.raises(OfflineBudgetExceeded, match="durable_actions"):
        ledger.reserve_durable_action()
    assert AITurnLimits().provider_calls == 3
    assert AITurnLimits().tool_rounds == 2
    assert AITurnLimits().capability_executions == 3
    assert classify_latency(5_000) == OfflineLatencyClass.one_call_target
    assert classify_latency(6_000) == OfflineLatencyClass.routine_target
    assert classify_latency(10_000) == OfflineLatencyClass.warning
    assert classify_latency(12_000) == OfflineLatencyClass.provider_deadline
    assert classify_latency(15_000) == OfflineLatencyClass.safe_boundary
    assert classify_latency(60_000) == OfflineLatencyClass.outer_watchdog

    settings = get_settings()
    assert settings.ai_adapter == "disabled"
    assert settings.ai_turn_provider == "disabled"
    assert settings.whatsapp_send_enabled is False
    assert settings.crm_send_enabled is False
    assert settings.payment_send_enabled is False
    assert settings.relance_enabled is False
    assert settings.scheduled_tasks_enabled is False
    assert settings.m1_maps_fanout_enabled is False
    assert not os.environ.get("DEEPSEEK_API_KEY")
    await _assert_protected_unchanged(factory, baseline)


@pytest.mark.asyncio
async def test_b1_o06_multilingual_evidence(postgres, monkeypatch) -> None:
    factory, _truth, baseline = postgres
    fixtures = (
        (
            "French",
            "Je cherche un air fryer pour ma famille.",
            "Réponse française préservée.",
        ),
        (
            "Noisy French",
            "stp frè, air fryer pr 4, budget 60$",
            "Réponse informelle préservée.",
        ),
        (
            "Lingala",
            "Nalingi air fryer mpo na libota, budget na ngai ezali 60 dollars.",
            "Eyano ya Lingala ebatelami.",
        ),
        (
            "Lingala/French",
            "Ndeko, compare-moi 4L na 6L; nini ekoki mpo na bato minei?",
            "Eyano Lingala/français ebatelami.",
        ),
        (
            "Swahili",
            "Natafuta air fryer kwa familia ya watu wanne, bajeti yangu ni dola 60.",
            "Jibu la Kiswahili limehifadhiwa.",
        ),
        (
            "Swahili/French",
            "Finalement bajeti ni 45 dollars; una option moins chère?",
            "Jibu la Kiswahili/français limehifadhiwa.",
        ),
    )

    def response_step(text_value: str):
        def respond(_request: ProviderTurnRequest) -> ProviderTurnResult:
            return ProviderTurnResult(
                text=text_value,
                finish_reason=ProviderFinishReason.completed,
            )

        return respond

    adapter = RecordingScriptedProvider(
        tuple(ScriptedProviderStep(response_step(output)) for _, _, output in fixtures)
    )
    await _install_closed_runtime(monkeypatch, factory, adapter)
    transcript_evidence: list[dict[str, str]] = []
    for index, (label, customer_text, response_text) in enumerate(fixtures):
        phone = f"+2438100052{index:02d}"
        result, _, _ = await _run_m1(
            phone=phone,
            content=customer_text,
            timestamp=datetime(2026, 9, 1, 9, tzinfo=timezone.utc)
            + timedelta(minutes=index),
        )
        assert result["status"] == "processed"
        _conversation, messages, audits, tickets = await _stored_state(factory, phone)
        assert [item.content for item in messages if item.direction == "inbound"] == [
            customer_text
        ]
        assert [item.content for item in messages if item.direction == "outbound"] == [
            response_text
        ]
        assert len(audits) == 1 and tickets == []
        transcript_evidence.append(
            {
                "language_fixture": label,
                "customer": customer_text,
                "response": response_text,
            }
        )

    serialized = redacted_evidence_json(
        {
            "contract": "mbb-ai5b-contract-v2",
            "transcripts": transcript_evidence,
            "reasoning_content": HIDDEN_REASONING_SENTINEL,
            "secret": HIDDEN_REASONING_SENTINEL,
        }
    )
    assert HIDDEN_REASONING_SENTINEL not in serialized
    assert "reasoning_content" not in serialized and "secret" not in serialized
    for _label, customer_text, response_text in fixtures:
        assert customer_text in serialized and response_text in serialized
    assert json.loads(serialized)["transcripts"] == transcript_evidence
    assert len(adapter.requests) == 6 and adapter.network_calls == 0
    await _assert_protected_unchanged(factory, baseline)


@pytest.mark.asyncio
async def test_b1_o07_timeout_and_late_result(postgres, monkeypatch) -> None:
    factory, _truth, baseline = postgres

    class SyntheticTimeoutTransport:
        calls = 0

        async def create_chat_completion(self, _payload):
            self.calls += 1
            raise httpx.ReadTimeout(
                "synthetic timeout",
                request=httpx.Request(
                    "POST", "https://api.deepseek.com/chat/completions"
                ),
            )

    transport = SyntheticTimeoutTransport()
    deepseek = DeepSeekAdapter(
        api_key="synthetic-not-a-credential", transport=transport
    )
    probe_request = ProviderTurnRequest(
        messages=(ProviderMessage(role="user", content="Synthetic timeout probe"),),
        system_instruction="Synthetic MBB timeout normalization probe.",
        max_output_tokens=512,
        reasoning_profile=ProviderReasoningProfile.default,
    )
    with pytest.raises(ProviderTurnError) as normalized:
        await deepseek.generate_turn(probe_request)
    assert normalized.value.category == ProviderErrorCategory.timeout
    assert transport.calls == 1

    def timeout_step(_request: ProviderTurnRequest) -> ProviderTurnResult:
        raise normalized_timeout()

    late = ProviderTurnResult(
        text=LATE_RESULT_SENTINEL,
        finish_reason=ProviderFinishReason.completed,
    )
    adapter = RecordingScriptedProvider(
        (
            ScriptedProviderStep(
                timeout_step,
                represented_latency_ms=AI5B1_PROVIDER_DEADLINE_SECONDS * 1_000,
                late_result=late,
            ),
        )
    )
    await _install_closed_runtime(monkeypatch, factory, adapter)
    result, source_message_id, _ = await _run_m1(
        phone="+243810005109",
        content="Quel est le prix actuel ?",
    )
    conversation, messages, audits, tickets = await _stored_state(
        factory, "+243810005109"
    )
    outbound = [item for item in messages if item.direction == "outbound"]
    assert result["status"] == "processed"
    assert result["processing_ms"] < AI5B1_SAFE_BOUNDARY_SECONDS * 1_000
    assert len(outbound) == 1 and outbound[0].content == t("error_fallback", "french")
    assert len(audits) == 1 and audits[0].source_message_id == source_message_id
    assert audits[0].outcome == "fallback_used"
    assert audits[0].safe_code == "timeout"
    assert tickets == []
    assert conversation.ai_execution_state == "eligible"
    assert adapter.evidence[0].failure_code == "timeout"
    assert adapter.evidence[0].latency_ms == 12_000
    assert adapter.evidence[0].latency_class == OfflineLatencyClass.provider_deadline
    assert adapter.rejected_late_results == 1
    assert adapter.network_calls == 0
    assert all(LATE_RESULT_SENTINEL not in item.content for item in messages)
    assert classify_latency(AI5B1_SAFE_BOUNDARY_SECONDS * 1_000) == (
        OfflineLatencyClass.safe_boundary
    )
    assert classify_latency(AI5B1_OUTER_WATCHDOG_SECONDS * 1_000) == (
        OfflineLatencyClass.outer_watchdog
    )
    await _assert_protected_unchanged(factory, baseline)
