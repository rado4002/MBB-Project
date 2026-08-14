from __future__ import annotations

import asyncio
import inspect
import json
import os
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app import database
from app.adapters.base import ProviderTurnAdapter
from app.ai.capabilities import TrustedCapabilityContext
from app.ai.policy import AI_SYSTEM_POLICY_VERSION
from app.ai.provider_contract import (
    ProviderContinuationState,
    ProviderFinishReason,
    ProviderToolCall,
    ProviderToolResult,
    ProviderTurnRequest,
    ProviderTurnResult,
)
from app.models.ai_turn_audit import AITurnAudit
from app.models.catalog import Product, SellableItem
from app.models.conversation import Conversation
from app.models.escalation_ticket import EscalationTicket
from app.models.message import Message
from app.models.operator_account import OperatorAccount
from app.models.order import Order
from app.models.payment import Payment
from app.modules.catalog.service import create_product, create_sellable_item
from app.modules.commerce_admin import CommerceAdminContext
from app.modules.inventory.service import set_inventory_status
from app.modules.m4_conversation.ownership import transition_ownership
from app.modules.pricing.service import (
    set_current_exchange_rate,
    set_current_usd_price,
)

DATABASE_URL = os.environ.get("AI3F_TEST_DATABASE_URL")
HIDDEN_REASONING_SENTINEL = "DO_NOT_PERSIST_REASONING_SENTINEL"
INTERNAL_NOTES_SENTINEL = "DO_NOT_PERSIST_INTERNAL_NOTES"
TRUSTED_ARGUMENT_NAMES = {
    "actor",
    "actor_id",
    "allowed_tools",
    "allowlist",
    "authorization",
    "business_id",
    "conversation_id",
    "customer_id",
    "expected_ownership_version",
    "human_owner_account_id",
    "internal_account_id",
    "owner_id",
    "owner_type",
    "ownership_version",
    "permissions",
    "tenant_id",
    "transaction_session",
    "turn_id",
}

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="AI3F_TEST_DATABASE_URL is required for disposable PostgreSQL evidence",
)

TRUNCATE = text(
    """
    TRUNCATE TABLE
        mbb.ai_turn_audits,
        mbb.conversation_ownership_idempotency,
        mbb.escalation_tickets,
        mbb.messages,
        mbb.payments,
        mbb.orders,
        mbb.leads,
        mbb.conversations,
        mbb.customers,
        mbb.product_media,
        mbb.inventory_statuses,
        mbb.exchange_rates,
        mbb.sellable_item_prices,
        mbb.sellable_items,
        mbb.products,
        mbb.operator_audit_security_metadata,
        mbb.operator_audit_events,
        mbb.operator_accounts
    RESTART IDENTITY CASCADE
    """
)


@dataclass(frozen=True)
class _BusinessTruth:
    operator_account_id: uuid.UUID
    available_item_id: uuid.UUID
    unavailable_item_id: uuid.UUID


@dataclass
class _RuntimeEvidence:
    authority_contexts: list[TrustedCapabilityContext]
    authority_results: list[bool]
    send_boundaries: list[uuid.UUID]


ProviderStep = Callable[
    [ProviderTurnRequest],
    ProviderTurnResult | Awaitable[ProviderTurnResult],
]


class _ScriptedProvider(ProviderTurnAdapter):
    provider_name = "scripted"
    model = "offline-ai3f"

    def __init__(self, *steps: ProviderStep) -> None:
        self._steps = steps
        self.requests: list[ProviderTurnRequest] = []
        self.observed_tool_results: list[ProviderToolResult] = []

    async def generate_turn(self, request: ProviderTurnRequest) -> ProviderTurnResult:
        _assert_provider_request_is_untrusted(request)
        self.requests.append(request)
        index = len(self.requests) - 1
        if index >= len(self._steps):
            pytest.fail("scripted provider exceeded its fixed response sequence")
        result = self._steps[index](request)
        if inspect.isawaitable(result):
            result = await result
        assert isinstance(result, ProviderTurnResult)
        return result


class _Task:
    request = SimpleNamespace(retries=0)

    def retry(self, **_kwargs):
        pytest.fail("offline AI integration unexpectedly requested a Celery retry")


def _admin(account_id: uuid.UUID) -> CommerceAdminContext:
    return CommerceAdminContext(
        actor_account_id=account_id,
        request_id="ai3f-fictional-product-seed",
    )


async def _seed_business_truth(
    factory: async_sessionmaker,
) -> _BusinessTruth:
    now = datetime(2026, 8, 14, 12, tzinfo=timezone.utc)
    account = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="ai3f.admin",
        display_name="AI-3F Fictional Administrator",
        email_normalized=None,
        password_hash="not-used",
        role="administrator",
        status="active",
        auth_version=1,
        must_change_password=False,
        temporary_password_expires_at=None,
        password_changed_at=now,
        last_login_at=None,
        created_at=now,
        updated_at=now,
    )
    async with factory() as session:
        session.add(account)
        await session.commit()

    async with factory() as session:
        product = await create_product(
            session,
            name="Fictional Air Fryer",
            category_code="air_fryer",
            description="Fictional six-litre family air fryer.",
            active=True,
            administrator=_admin(account.account_id),
        )
        available = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="6L",
            sku="AI3F-FRYER-6L",
            attributes={"capacity_l": 6},
            active=True,
            administrator=_admin(account.account_id),
        )
        unavailable = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="8L",
            sku="AI3F-FRYER-8L",
            attributes={"capacity_l": 8},
            active=True,
            administrator=_admin(account.account_id),
        )
        await set_current_usd_price(
            session,
            sellable_item_id=available.sellable_item_id,
            amount=Decimal("55.00"),
            administrator=_admin(account.account_id),
        )
        await set_current_usd_price(
            session,
            sellable_item_id=unavailable.sellable_item_id,
            amount=Decimal("70.00"),
            administrator=_admin(account.account_id),
        )
        await set_inventory_status(
            session,
            sellable_item_id=available.sellable_item_id,
            status="available",
            administrator=_admin(account.account_id),
        )
        await set_inventory_status(
            session,
            sellable_item_id=unavailable.sellable_item_id,
            status="out_of_stock",
            administrator=_admin(account.account_id),
        )
        await set_current_exchange_rate(
            session,
            base_currency="USD",
            quote_currency="CDF",
            rate=Decimal("2800.000000"),
            administrator=_admin(account.account_id),
        )
        await session.commit()

    return _BusinessTruth(
        operator_account_id=account.account_id,
        available_item_id=available.sellable_item_id,
        unavailable_item_id=unavailable.sellable_item_id,
    )


@pytest_asyncio.fixture(loop_scope="function")
async def postgres() -> tuple[async_sessionmaker, _BusinessTruth]:
    assert DATABASE_URL is not None
    engine: AsyncEngine = create_async_engine(DATABASE_URL, pool_size=8)
    async with engine.begin() as connection:
        await connection.execute(TRUNCATE)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        truth = await _seed_business_truth(factory)
        yield factory, truth
    finally:
        async with engine.begin() as connection:
            await connection.execute(TRUNCATE)
        await engine.dispose()


def _continuation(phase: str) -> ProviderContinuationState:
    return ProviderContinuationState(
        value={
            "hidden_reasoning": HIDDEN_REASONING_SENTINEL,
            "opaque_internal_note": INTERNAL_NOTES_SENTINEL,
            "phase": phase,
        }
    )


def _tool_result(request: ProviderTurnRequest, call_id: str) -> ProviderToolResult:
    matches = [
        ProviderToolResult.model_validate_json(message.content)
        for message in request.messages
        if message.role == "tool_result" and message.tool_call_id == call_id
    ]
    assert len(matches) == 1
    return matches[0]


def _assert_provider_request_is_untrusted(request: ProviderTurnRequest) -> None:
    assert not hasattr(request, "conversation_id")
    assert not hasattr(request, "customer_id")
    assert not hasattr(request, "ownership_version")
    assert "transaction_session" not in request.model_dump_json()
    for capability in request.allowed_capabilities:
        properties = capability.input_schema.get("properties", {})
        assert TRUSTED_ARGUMENT_NAMES.isdisjoint(properties)


async def _install_runtime(
    monkeypatch: pytest.MonkeyPatch,
    factory: async_sessionmaker,
    adapter: _ScriptedProvider,
) -> _RuntimeEvidence:
    import app.adapters as adapters
    import app.ai.turn as turn_module
    import app.modules.m1_gateway.session_cache as session_cache
    import app.modules.m4_conversation.engine as conversation_engine
    from app.tasks import m1

    evidence = _RuntimeEvidence(
        authority_contexts=[],
        authority_results=[],
        send_boundaries=[],
    )
    monkeypatch.setattr(database, "async_session_factory", factory)
    monkeypatch.setattr(adapters, "get_provider_turn_adapter", lambda: adapter)
    monkeypatch.setattr(
        adapters,
        "get_ai_adapter",
        lambda: pytest.fail("legacy get_ai_adapter was used by the AI-3F path"),
    )
    monkeypatch.setattr(
        adapters,
        "get_messaging_adapter",
        lambda: pytest.fail("real messaging adapter was resolved"),
    )

    original_authority_check = turn_module._ai_authority_is_current

    async def record_authority(context: TrustedCapabilityContext) -> bool:
        evidence.authority_contexts.append(context)
        result = await original_authority_check(context)
        evidence.authority_results.append(result)
        return result

    monkeypatch.setattr(turn_module, "_ai_authority_is_current", record_authority)

    async def no_cached_session(_conversation_id: str):
        return None

    async def save_session(_conversation_id: str, _state) -> bool:
        return True

    monkeypatch.setattr(session_cache, "get_session", no_cached_session)
    monkeypatch.setattr(session_cache, "save_session", save_session)
    monkeypatch.setattr(
        conversation_engine,
        "detect_qualification_signals",
        lambda _content: False,
    )
    monkeypatch.setattr(
        m1.celery_app,
        "send_task",
        lambda *_args, **_kwargs: pytest.fail("MAPS/Celery fanout was dispatched"),
    )
    monkeypatch.setattr(
        m1,
        "settings",
        SimpleNamespace(
            whatsapp_send_enabled=False,
            m1_maps_fanout_enabled=False,
        ),
    )

    original_send_safe = m1._send_safe

    async def observe_send_boundary(
        phone: str,
        content: str,
        *,
        idempotency_key: str,
        conversation_id: uuid.UUID | None = None,
        expected_ownership_version: int | None = None,
    ) -> dict[str, str]:
        outbound_id = uuid.UUID(idempotency_key)
        async with factory() as session:
            assert await session.get(Message, outbound_id) is not None
            assert await session.scalar(
                select(func.count()).select_from(AITurnAudit).where(
                    AITurnAudit.outbound_message_id == outbound_id
                )
            ) == 1
        evidence.send_boundaries.append(outbound_id)
        return await original_send_safe(
            phone,
            content,
            idempotency_key=idempotency_key,
            conversation_id=conversation_id,
            expected_ownership_version=expected_ownership_version,
        )

    monkeypatch.setattr(m1, "_send_safe", observe_send_boundary)
    return evidence


async def _run_m1(*, phone: str, content: str) -> tuple[dict, uuid.UUID]:
    from app.tasks import m1

    source_message_id = uuid.uuid4()
    result = await m1._process(
        task=_Task(),
        message_id=str(source_message_id),
        customer_phone=phone,
        content=content,
        content_type="text",
        timestamp="2026-08-14T12:00:00+00:00",
        whatsapp_message_id=f"ai3f-{source_message_id}",
    )
    return result, source_message_id


async def _stored_state(
    factory: async_sessionmaker,
    phone: str,
) -> tuple[Conversation, list[Message], list[AITurnAudit], list[EscalationTicket]]:
    async with factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.customer_id == phone)
        )
        assert conversation is not None
        messages = list(
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation.conversation_id)
                .order_by(Message.timestamp, Message.direction)
            )
        )
        audits = list(
            await session.scalars(
                select(AITurnAudit).where(
                    AITurnAudit.conversation_id == conversation.conversation_id
                )
            )
        )
        tickets = list(
            await session.scalars(
                select(EscalationTicket).where(
                    EscalationTicket.conversation_id == conversation.conversation_id
                )
            )
        )
    return conversation, messages, audits, tickets


def _assert_audit_privacy(
    audit: AITurnAudit,
    *,
    customer_content: str,
    provider_text: str | None = None,
    product_truth_must_be_absent: bool = False,
) -> None:
    serialized = json.dumps(
        {column.name: getattr(audit, column.name) for column in audit.__table__.columns},
        default=str,
        sort_keys=True,
    )
    for forbidden in (
        HIDDEN_REASONING_SENTINEL,
        INTERNAL_NOTES_SENTINEL,
        customer_content,
        "Traceback",
        "api_key",
        "authorization_header",
    ):
        assert forbidden not in serialized
    if provider_text is not None:
        assert provider_text not in serialized
    if product_truth_must_be_absent:
        assert "Fictional Air Fryer" not in serialized
        assert "55.00" not in serialized


def _assert_requests_exclude_authority_values(
    adapter: _ScriptedProvider,
    *,
    conversation: Conversation,
    customer_id: str,
    turn_id: uuid.UUID,
) -> None:
    serialized = "\n".join(request.model_dump_json() for request in adapter.requests)
    assert str(conversation.conversation_id) not in serialized
    assert customer_id not in serialized
    assert str(turn_id) not in serialized


def _assert_common_audit(
    audit: AITurnAudit,
    *,
    conversation: Conversation,
    source_message_id: uuid.UUID,
    outbound_message_id: uuid.UUID | None,
) -> None:
    assert audit.conversation_id == conversation.conversation_id
    assert audit.source_message_id == source_message_id
    assert audit.outbound_message_id == outbound_message_id
    assert audit.actor_type == "ai"
    assert audit.actor_id == "mbb_ai"
    assert audit.policy_version == AI_SYSTEM_POLICY_VERSION
    assert audit.provider == "scripted"
    assert audit.model == "offline-ai3f"
    assert set(audit.exposed_capabilities) == {
        "get_product_details",
        "request_human_handoff",
        "search_products",
    }


@pytest.mark.asyncio
async def test_scenario_a_product_inquiry_through_real_m1(
    postgres,
    monkeypatch,
) -> None:
    factory, truth = postgres
    observed_item: dict[str, object] = {}

    def request_search(request: ProviderTurnRequest) -> ProviderTurnResult:
        _assert_provider_request_is_untrusted(request)
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="scenario_a_search",
                    capability_name="search_products",
                    arguments={
                        "query": "air fryer",
                        "max_budget": 70,
                        "budget_currency": "USD",
                    },
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("scenario_a_search"),
        )

    def finish_from_truth(request: ProviderTurnRequest) -> ProviderTurnResult:
        assert request.continuation_state is not None
        assert request.continuation_state.value["hidden_reasoning"] == (
            HIDDEN_REASONING_SENTINEL
        )
        tool_result = _tool_result(request, "scenario_a_search")
        adapter.observed_tool_results.append(tool_result)
        assert tool_result.status == "success"
        assert tool_result.output is not None
        items = tool_result.output["items"]
        assert isinstance(items, list) and len(items) == 1
        observed_item.update(items[0])
        final_text = (
            f"{observed_item['name']} {observed_item['model_label']} coute "
            f"{observed_item['current_usd_price']} USD et est disponible."
        )
        return ProviderTurnResult(
            text=final_text,
            finish_reason=ProviderFinishReason.completed,
        )

    adapter = _ScriptedProvider(request_search, finish_from_truth)
    evidence = await _install_runtime(monkeypatch, factory, adapter)
    customer_content = "Je cherche un air fryer a moins de 70 USD."
    result, source_message_id = await _run_m1(
        phone="+243810000101",
        content=customer_content,
    )

    conversation, messages, audits, tickets = await _stored_state(
        factory,
        "+243810000101",
    )
    assert result["status"] == "processed"
    assert result["send_status"] == "skipped"
    assert len(adapter.requests) == 2
    assert len(adapter.observed_tool_results) == 1
    assert observed_item["sellable_item_id"] == str(truth.available_item_id)
    assert Decimal(str(observed_item["current_usd_price"])) == Decimal("55.00")
    assert observed_item["availability"] == "available"
    assert observed_item["offer_status"] == "sellable_now"
    assert sum(message.direction == "inbound" for message in messages) == 1
    assert sum(message.direction == "outbound" for message in messages) == 1
    assert len(audits) == 1
    assert tickets == []
    outbound = next(message for message in messages if message.direction == "outbound")
    assert outbound.content == (
        f"{observed_item['name']} {observed_item['model_label']} coute "
        f"{observed_item['current_usd_price']} USD et est disponible."
    )
    audit = audits[0]
    _assert_common_audit(
        audit,
        conversation=conversation,
        source_message_id=source_message_id,
        outbound_message_id=outbound.message_id,
    )
    assert audit.outcome == "response_generated"
    assert audit.capability_activity == [
        {
            "capability_name": "search_products",
            "decision": "executed",
            "outcome": "success",
            "safe_code": None,
        }
    ]
    assert evidence.send_boundaries == [outbound.message_id]
    assert len(evidence.authority_contexts) == 4
    assert evidence.authority_results == [True, True, True, True]
    assert all(
        context.conversation_id == conversation.conversation_id
        and context.expected_ownership_version == 1
        and context.turn_id == audit.turn_id
        for context in evidence.authority_contexts
    )
    assert set(TrustedCapabilityContext.__dataclass_fields__) == {
        "conversation_id",
        "turn_id",
        "expected_ownership_version",
    }
    _assert_requests_exclude_authority_values(
        adapter,
        conversation=conversation,
        customer_id="+243810000101",
        turn_id=audit.turn_id,
    )
    _assert_audit_privacy(
        audit,
        customer_content=customer_content,
        provider_text=outbound.content,
        product_truth_must_be_absent=True,
    )


@pytest.mark.asyncio
async def test_scenario_b_two_round_product_continuation_is_bounded(
    postgres,
    monkeypatch,
) -> None:
    factory, truth = postgres
    observed_detail: dict[str, object] = {}

    def request_search(request: ProviderTurnRequest) -> ProviderTurnResult:
        assert request.continuation_state is None
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="scenario_b_search",
                    capability_name="search_products",
                    arguments={"query": "air fryer", "limit": 2},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("scenario_b_search"),
        )

    def request_details(request: ProviderTurnRequest) -> ProviderTurnResult:
        assert request.continuation_state is not None
        assert request.continuation_state.value["phase"] == "scenario_b_search"
        search_result = _tool_result(request, "scenario_b_search")
        adapter.observed_tool_results.append(search_result)
        assert search_result.status == "success"
        assert search_result.output is not None
        items = search_result.output["items"]
        assert isinstance(items, list) and len(items) == 1
        item = items[0]
        assert item["sellable_item_id"] == str(truth.available_item_id)
        assert item["sellable_item_id"] != str(truth.unavailable_item_id)
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="scenario_b_details",
                    capability_name="get_product_details",
                    arguments={"sellable_item_id": item["sellable_item_id"]},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("scenario_b_details"),
        )

    def finish_from_details(request: ProviderTurnRequest) -> ProviderTurnResult:
        assert request.continuation_state is not None
        assert request.continuation_state.value["phase"] == "scenario_b_details"
        details_result = _tool_result(request, "scenario_b_details")
        adapter.observed_tool_results.append(details_result)
        assert details_result.status == "success"
        assert details_result.output is not None
        observed_detail.update(details_result.output["product"])
        return ProviderTurnResult(
            text=(
                f"Le modele {observed_detail['model_label']} coute "
                f"{observed_detail['current_usd_price']} USD; reference "
                f"{observed_detail['sku']}."
            ),
            finish_reason=ProviderFinishReason.completed,
        )

    adapter = _ScriptedProvider(
        request_search,
        request_details,
        finish_from_details,
    )
    evidence = await _install_runtime(monkeypatch, factory, adapter)
    customer_content = "Donnez-moi les details de votre air fryer disponible."
    result, source_message_id = await _run_m1(
        phone="+243810000102",
        content=customer_content,
    )

    conversation, messages, audits, _tickets = await _stored_state(
        factory,
        "+243810000102",
    )
    outbound = next(message for message in messages if message.direction == "outbound")
    assert result["status"] == "processed"
    assert result["send_status"] == "skipped"
    assert len(adapter.requests) == 3
    assert len(adapter.observed_tool_results) == 2
    assert sum(message.direction == "inbound" for message in messages) == 1
    assert sum(message.direction == "outbound" for message in messages) == 1
    assert observed_detail["sellable_item_id"] == str(truth.available_item_id)
    assert observed_detail["sku"] == "AI3F-FRYER-6L"
    assert Decimal(str(observed_detail["current_usd_price"])) == Decimal("55.00")
    assert outbound.content == (
        f"Le modele {observed_detail['model_label']} coute "
        f"{observed_detail['current_usd_price']} USD; reference "
        f"{observed_detail['sku']}."
    )
    assert len(audits) == 1
    audit = audits[0]
    _assert_common_audit(
        audit,
        conversation=conversation,
        source_message_id=source_message_id,
        outbound_message_id=outbound.message_id,
    )
    assert audit.outcome == "response_generated"
    assert audit.capability_activity == [
        {
            "capability_name": "search_products",
            "decision": "executed",
            "outcome": "success",
            "safe_code": None,
        },
        {
            "capability_name": "get_product_details",
            "decision": "executed",
            "outcome": "success",
            "safe_code": None,
        },
    ]
    assert evidence.send_boundaries == [outbound.message_id]
    assert len(evidence.authority_contexts) == 6
    assert evidence.authority_results == [True, True, True, True, True, True]
    assert all(
        context.conversation_id == conversation.conversation_id
        and context.expected_ownership_version == 1
        and context.turn_id == audit.turn_id
        for context in evidence.authority_contexts
    )
    assert set(TrustedCapabilityContext.__dataclass_fields__) == {
        "conversation_id",
        "turn_id",
        "expected_ownership_version",
    }
    _assert_requests_exclude_authority_values(
        adapter,
        conversation=conversation,
        customer_id="+243810000102",
        turn_id=audit.turn_id,
    )
    _assert_audit_privacy(
        audit,
        customer_content=customer_content,
        provider_text=outbound.content,
        product_truth_must_be_absent=True,
    )


@pytest.mark.asyncio
async def test_scenario_c_human_handoff_is_terminal_through_real_m1(
    postgres,
    monkeypatch,
) -> None:
    factory, _truth = postgres

    def request_handoff(_request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="scenario_c_handoff",
                    capability_name="request_human_handoff",
                    arguments={"reason_category": "customer_requested_human"},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("scenario_c_handoff"),
        )

    adapter = _ScriptedProvider(request_handoff)
    evidence = await _install_runtime(monkeypatch, factory, adapter)
    customer_content = "Je veux parler a une personne."
    result, source_message_id = await _run_m1(
        phone="+243810000103",
        content=customer_content,
    )

    conversation, messages, audits, tickets = await _stored_state(
        factory,
        "+243810000103",
    )
    assert result == {
        "status": "waiting_for_human",
        "conversation_id": str(conversation.conversation_id),
        "send_status": "skipped",
    }
    assert len(adapter.requests) == 1
    assert sum(message.direction == "inbound" for message in messages) == 1
    assert sum(message.direction == "outbound" for message in messages) == 0
    assert conversation.owner_type == "ai"
    assert conversation.human_owner_account_id is None
    assert conversation.ai_execution_state == "paused"
    assert conversation.ownership_version == 2
    assert len(tickets) == 1
    assert tickets[0].status == "open"
    assert tickets[0].source == "ai_capability"
    assert tickets[0].escalation_type == "human_handoff"
    assert len(audits) == 1
    audit = audits[0]
    _assert_common_audit(
        audit,
        conversation=conversation,
        source_message_id=source_message_id,
        outbound_message_id=None,
    )
    assert audit.outcome == "handoff_requested"
    assert audit.capability_activity == [
        {
            "capability_name": "request_human_handoff",
            "decision": "executed",
            "outcome": "success",
            "safe_code": None,
        }
    ]
    assert evidence.send_boundaries == []
    assert len(evidence.authority_contexts) == 2
    assert evidence.authority_results == [True, True]
    assert all(
        context.conversation_id == conversation.conversation_id
        and context.expected_ownership_version == 1
        and context.turn_id == audit.turn_id
        for context in evidence.authority_contexts
    )
    _assert_requests_exclude_authority_values(
        adapter,
        conversation=conversation,
        customer_id="+243810000103",
        turn_id=audit.turn_id,
    )
    _assert_audit_privacy(audit, customer_content=customer_content)


@pytest.mark.asyncio
async def test_scenario_d_human_takeover_rejects_pending_provider_result(
    postgres,
    monkeypatch,
) -> None:
    factory, truth = postgres
    inference_started = asyncio.Event()
    release_result = asyncio.Event()

    async def delayed_final(_request: ProviderTurnRequest) -> ProviderTurnResult:
        inference_started.set()
        await release_result.wait()
        return ProviderTurnResult(
            text="Cette reponse stale ne doit jamais persister.",
            finish_reason=ProviderFinishReason.completed,
            continuation_state=_continuation("scenario_d_stale"),
        )

    adapter = _ScriptedProvider(delayed_final)
    evidence = await _install_runtime(monkeypatch, factory, adapter)
    phone = "+243810000104"
    processing = asyncio.create_task(
        _run_m1(
            phone=phone,
            content="Donnez-moi le prix pendant cette prise de controle.",
        )
    )
    try:
        await asyncio.wait_for(inference_started.wait(), timeout=10)
        async with factory() as session:
            conversation = await session.scalar(
                select(Conversation).where(Conversation.customer_id == phone)
            )
            assert conversation is not None
            takeover = await transition_ownership(
                session,
                conversation_id=conversation.conversation_id,
                target_owner_type="human",
                expected_version=1,
                actor_account_id=truth.operator_account_id,
                actor_display_name="AI-3F Fictional Administrator",
                actor_role="administrator",
                idempotency_key=uuid.uuid4(),
                idempotency_secret="ai3f-human-takeover-secret-" + ("x" * 32),
                request_id="ai3f-authority-loss",
                ai_adapter="claude",
            )
        assert takeover.ownership.owner_type == "human"
        release_result.set()
        result, source_message_id = await asyncio.wait_for(processing, timeout=10)
    except BaseException:
        release_result.set()
        processing.cancel()
        await asyncio.gather(processing, return_exceptions=True)
        raise

    conversation, messages, audits, tickets = await _stored_state(factory, phone)
    assert result["status"] == "persistence_failed"
    assert result["send_status"] == "unknown_or_failed"
    assert len(adapter.requests) == 1
    assert conversation.owner_type == "human"
    assert conversation.human_owner_account_id == truth.operator_account_id
    assert conversation.ai_execution_state == "paused"
    assert conversation.ownership_version == 2
    assert sum(message.direction == "inbound" for message in messages) == 1
    assert next(message for message in messages if message.direction == "inbound").message_id == (
        source_message_id
    )
    assert sum(message.direction == "outbound" for message in messages) == 0
    assert audits == []
    assert tickets == []
    assert evidence.send_boundaries == []
    assert len(evidence.authority_contexts) == 2
    assert evidence.authority_results == [True, False]
    assert all(
        context.conversation_id == conversation.conversation_id
        and context.expected_ownership_version == 1
        and context.turn_id == evidence.authority_contexts[0].turn_id
        for context in evidence.authority_contexts
    )
    _assert_requests_exclude_authority_values(
        adapter,
        conversation=conversation,
        customer_id=phone,
        turn_id=evidence.authority_contexts[0].turn_id,
    )


@pytest.mark.asyncio
async def test_scenario_e_safe_missing_product_failure_continues_truthfully(
    postgres,
    monkeypatch,
) -> None:
    factory, _truth = postgres
    missing_item_id = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
    observed_error: dict[str, object] = {}

    def request_missing_details(_request: ProviderTurnRequest) -> ProviderTurnResult:
        return ProviderTurnResult(
            tool_calls=(
                ProviderToolCall(
                    call_id="scenario_e_missing",
                    capability_name="get_product_details",
                    arguments={"sellable_item_id": str(missing_item_id)},
                ),
            ),
            finish_reason=ProviderFinishReason.tool_call,
            continuation_state=_continuation("scenario_e_missing"),
        )

    def truthful_failure(request: ProviderTurnRequest) -> ProviderTurnResult:
        tool_result = _tool_result(request, "scenario_e_missing")
        adapter.observed_tool_results.append(tool_result)
        assert tool_result.status == "error"
        assert tool_result.error is not None
        observed_error.update(tool_result.error.model_dump(mode="json"))
        return ProviderTurnResult(
            text=(
                "Je ne trouve pas ce produit dans les donnees MBB actuelles; "
                "je ne peux confirmer ni prix ni stock."
            ),
            finish_reason=ProviderFinishReason.completed,
        )

    adapter = _ScriptedProvider(request_missing_details, truthful_failure)
    evidence = await _install_runtime(monkeypatch, factory, adapter)
    customer_content = "Confirmez le produit fictif Z99."
    result, source_message_id = await _run_m1(
        phone="+243810000105",
        content=customer_content,
    )

    conversation, messages, audits, tickets = await _stored_state(
        factory,
        "+243810000105",
    )
    outbound = next(message for message in messages if message.direction == "outbound")
    assert result["status"] == "processed"
    assert result["send_status"] == "skipped"
    assert len(adapter.requests) == 2
    assert len(adapter.observed_tool_results) == 1
    assert sum(message.direction == "inbound" for message in messages) == 1
    assert sum(message.direction == "outbound" for message in messages) == 1
    assert observed_error == {
        "category": "execution_failed",
        "safe_code": "sellable_item_not_found",
    }
    assert "ni prix ni stock" in outbound.content
    assert len(audits) == 1
    assert tickets == []
    audit = audits[0]
    _assert_common_audit(
        audit,
        conversation=conversation,
        source_message_id=source_message_id,
        outbound_message_id=outbound.message_id,
    )
    assert audit.outcome == "response_generated"
    assert audit.capability_activity == [
        {
            "capability_name": "get_product_details",
            "decision": "executed",
            "outcome": "failed",
            "safe_code": "sellable_item_not_found",
        }
    ]
    assert evidence.send_boundaries == [outbound.message_id]
    assert len(evidence.authority_contexts) == 4
    assert evidence.authority_results == [True, True, True, True]
    assert all(
        context.conversation_id == conversation.conversation_id
        and context.expected_ownership_version == 1
        and context.turn_id == audit.turn_id
        for context in evidence.authority_contexts
    )
    _assert_requests_exclude_authority_values(
        adapter,
        conversation=conversation,
        customer_id="+243810000105",
        turn_id=audit.turn_id,
    )
    async with factory() as session:
        assert await session.scalar(select(func.count()).select_from(Product)) == 1
        assert await session.scalar(select(func.count()).select_from(SellableItem)) == 2
        assert await session.scalar(select(func.count()).select_from(Order)) == 0
        assert await session.scalar(select(func.count()).select_from(Payment)) == 0
    _assert_audit_privacy(
        audit,
        customer_content=customer_content,
        provider_text=outbound.content,
    )
