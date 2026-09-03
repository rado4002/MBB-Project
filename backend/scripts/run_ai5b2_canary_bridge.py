"""Dry-run, offline-validate, or execute the guarded AI-5B2 canary stage."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.ai.deepseek_adapter import DeepSeekAdapter  # noqa: E402
from app.adapters.ai.disabled_adapter import DisabledAIAdapter  # noqa: E402
from app.adapters.base import ProviderTurnAdapter  # noqa: E402
from app.ai.canary_bridge import (  # noqa: E402
    AI5B2_CANARIES,
    AI5B2BridgeConfigurationError,
    AI5B2BudgetProfile,
    AI5B2ProviderSelection,
    C01CommercialFacts,
    CanaryAuthorizationRecord,
    CanaryBridgeEvidence,
    CanaryCaseEvidence,
    CanaryManualReviewStatus,
    CanaryPricingVerificationRecord,
    CanaryProviderMode,
    CanaryReviewerAssignmentRecord,
    CanaryStageStopLatch,
    CanaryTranscriptEntry,
    CumulativeBudgetProvider,
    conservative_json_request_reservation,
    dispatch_guarded_canary_stage,
    dry_run_manifest,
    evaluate_c01_commercial_response,
)
from app.ai.offline_certification import (  # noqa: E402
    EvaluationDeadlineAdapter,
    EvaluationOuterWatchdog,
    OfflineBudgetLedger,
    SystemEvaluationClock,
    redacted_evidence_json,
)
from app.config import get_settings  # noqa: E402
from scripts.run_ai5b1_offline_certification import (  # noqa: E402
    DisposablePostgresRuntime,
    main as run_disposable_suite,
)


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
SAFE_RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{7,63}$")


@dataclass(frozen=True)
class CanaryBusinessTruth:
    operator_account_id: uuid.UUID
    available_item_id: uuid.UUID
    unavailable_item_id: uuid.UUID
    available_product_name: str
    available_model_label: str
    available_usd_price: Decimal
    available_cdf_price: Decimal
    available_is_sellable_now: bool

    def evidence(self) -> dict[str, object]:
        return {
            "P6_sellable_item_id": str(self.available_item_id),
            "P6_price_usd": str(self.available_usd_price),
            "P6_price_cdf": str(self.available_cdf_price),
            "P6_availability": "available",
            "P6_is_sellable_now": self.available_is_sellable_now,
            "P8_sellable_item_id": str(self.unavailable_item_id),
            "P8_price_usd": "70.00",
            "P8_availability": "out_of_stock",
        }


@dataclass
class CanaryRuntimeEvidence:
    terminal_offer_reads: list[uuid.UUID] = field(default_factory=list)
    blocked_external_actions: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CanaryCLIOverrides:
    """In-process injection seam used only by offline CLI orchestration tests."""

    credential_loader: Callable[[], str]
    transport_builder: Callable[[str, CanaryBusinessTruth], Any]
    after_prepare: Callable[[DisposablePostgresRuntime], None] | None = None
    provider_mode: CanaryProviderMode = CanaryProviderMode.offline_mocked_http


class CanaryStageExecutionFailure(RuntimeError):
    def __init__(self, original: BaseException, evidence: dict[str, object]) -> None:
        self.original = original
        self.evidence = evidence
        super().__init__(type(original).__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline-postgres",
        action="store_true",
        help="run the existing focused offline PostgreSQL bridge suite",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="execute a separately authorized guarded run; never implied by credentials",
    )
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--authorized-baseline")
    parser.add_argument("--authorization-record-id")
    parser.add_argument("--pricing-record-id")
    parser.add_argument("--pricing-source")
    parser.add_argument("--pricing-verified-at")
    parser.add_argument("--input-usd-per-million", type=Decimal)
    parser.add_argument("--output-usd-per-million", type=Decimal)
    parser.add_argument("--reviewer-record-id")
    parser.add_argument("--reviewer-id")
    parser.add_argument("--reviewer-drc-language-familiarity", action="store_true")
    parser.add_argument("--external-effects-disabled", action="store_true")
    parser.add_argument("--evidence-root")
    return parser


def _credential_loader() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def _current_head() -> str:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _external_effects_are_disabled() -> bool:
    settings = get_settings()
    return not any(
        (
            settings.whatsapp_send_enabled,
            settings.crm_send_enabled,
            settings.payment_send_enabled,
            settings.relance_enabled,
            settings.scheduled_tasks_enabled,
            settings.m1_maps_fanout_enabled,
        )
    )


def _external_effect_guard_evidence() -> dict[str, object]:
    settings = get_settings()
    return {
        "AI_ADAPTER": settings.ai_adapter,
        "AI_TURN_PROVIDER": settings.ai_turn_provider,
        "WHATSAPP_SEND_ENABLED": settings.whatsapp_send_enabled,
        "CRM_SEND_ENABLED": settings.crm_send_enabled,
        "PAYMENT_SEND_ENABLED": settings.payment_send_enabled,
        "RELANCE_ENABLED": settings.relance_enabled,
        "SCHEDULED_TASKS_ENABLED": settings.scheduled_tasks_enabled,
        "M1_MAPS_FANOUT_ENABLED": settings.m1_maps_fanout_enabled,
        "external_business_effects_verified_disabled": (
            _external_effects_are_disabled()
        ),
    }


def _disposable_database_is_isolated() -> bool:
    database_url = os.environ.get("AI5B2_BRIDGE_TEST_DATABASE_URL", "")
    cluster_id = os.environ.get("AI5B2_BRIDGE_DISPOSABLE_CLUSTER_ID", "")
    if not database_url or not cluster_id.startswith("mbb-ai5b1-cluster-"):
        return False
    try:
        parsed = make_url(database_url)
        active = make_url(get_settings().database_url)
    except Exception:
        return False
    return bool(
        parsed.drivername == "postgresql+asyncpg"
        and parsed.host == "127.0.0.1"
        and parsed.port is not None
        and parsed.port != 5432
        and parsed.database is not None
        and parsed.database.startswith("ai5b1_cert_")
        and parsed.username == "ai5b1_admin"
        and parsed.password in {None, ""}
        and active.drivername == parsed.drivername
        and active.host == parsed.host
        and active.port == parsed.port
        and active.database == parsed.database
        and active.username == parsed.username
    )


class _CanaryTask:
    request = SimpleNamespace(retries=0)

    @staticmethod
    def retry(*, exc: Exception, **_kwargs):
        raise exc


def _evidence_directory(
    args: argparse.Namespace,
    run_id: str,
    runtime: DisposablePostgresRuntime,
) -> Path:
    if SAFE_RUN_ID.fullmatch(run_id) is None:
        raise AI5B2BridgeConfigurationError("invalid_run_id")
    repository_root = Path(__file__).resolve().parents[2]
    root = (
        Path(args.evidence_root).expanduser().resolve()
        if args.evidence_root
        else (Path(tempfile.gettempdir()) / "mbb-ai5b2-evidence").resolve()
    )
    directory = (root / run_id).resolve()
    if directory == repository_root or repository_root in directory.parents:
        raise AI5B2BridgeConfigurationError("evidence_directory_inside_repository")
    if directory == runtime.cluster_root or runtime.cluster_root in directory.parents:
        raise AI5B2BridgeConfigurationError(
            "evidence_directory_inside_database_runtime"
        )
    directory.mkdir(parents=True, exist_ok=False)
    return directory


def _write_atomic(path: Path, value: object) -> None:
    serialized = redacted_evidence_json(value)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text(serialized + "\n", encoding="utf-8")
    temporary.replace(path)


def build_c01_reevaluation(source_path: Path) -> dict[str, object]:
    """Re-evaluate immutable C01 evidence against its frozen authoritative facts."""
    source_bytes = source_path.read_bytes()
    source_hash = hashlib.sha256(source_bytes).hexdigest()
    source = json.loads(source_bytes)
    cases = source.get("cases")
    if not isinstance(cases, list):
        raise AI5B2BridgeConfigurationError("reevaluation_cases_missing")
    case = next(
        (
            item
            for item in cases
            if isinstance(item, dict) and item.get("case_id") == "B2-C01-FR-FRESH-P6"
        ),
        None,
    )
    if case is None:
        raise AI5B2BridgeConfigurationError("reevaluation_c01_missing")
    transcript = case.get("transcript")
    if not isinstance(transcript, list):
        raise AI5B2BridgeConfigurationError("reevaluation_transcript_missing")
    response = next(
        (
            item.get("content")
            for item in reversed(transcript)
            if isinstance(item, dict)
            and item.get("direction") == "outbound"
            and isinstance(item.get("content"), str)
        ),
        None,
    )
    if response is None:
        raise AI5B2BridgeConfigurationError("reevaluation_response_missing")
    result = evaluate_c01_commercial_response(
        response,
        facts=C01CommercialFacts(
            product_name="MBB Test Air Fryer",
            sellable_model_label="6L",
            usd_price=Decimal("55.00"),
            cdf_price=Decimal("154000.00"),
            availability="available",
            is_sellable_now=True,
            freshness_verified=case.get("freshness_verified") is True,
        ),
    )
    return {
        "reevaluation_type": "immutable_source_evidence_reevaluation",
        "evaluator_version": result.evaluator_version,
        "source_evidence_path": str(source_path.resolve()),
        "source_evidence_sha256": source_hash,
        "source_run_id": source.get("run_id"),
        "source_baseline_commit": source.get("baseline_commit"),
        "historical_result_unchanged": {
            "deterministic_status": case.get("deterministic_status"),
            "failure_attribution": case.get("failure_attribution"),
        },
        "authoritative_facts": {
            "product": "MBB Test Air Fryer 6L",
            "price_usd": "55.00",
            "price_cdf": "154000.00",
            "availability": "available",
            "freshness_verified": case.get("freshness_verified") is True,
        },
        "fact_provenance": {
            "fixture": "backend/scripts/run_ai5b2_canary_bridge.py",
            "conversion_rule": "backend/app/modules/pricing/service.py:calculate_cdf_amount",
            "provider_projection": "backend/app/ai/capabilities.py:_project_offer",
            "retained_tool_arguments_and_results": False,
        },
        "corrected_evaluation": result.model_dump(mode="json"),
        "human_language_review": "pending",
    }


def write_c01_reevaluation(source_path: Path, output_path: Path) -> None:
    _write_atomic(output_path, build_c01_reevaluation(source_path))


@contextmanager
def _activated_runtime(runtime: DisposablePostgresRuntime) -> Iterator[None]:
    names = {
        "POSTGRES_HOST": "127.0.0.1",
        "POSTGRES_PORT": str(runtime.port),
        "POSTGRES_DB": runtime.database_name,
        "POSTGRES_USER": "ai5b1_admin",
        "POSTGRES_PASSWORD": "",
        "AI5B2_BRIDGE_TEST_DATABASE_URL": runtime.database_url,
        "AI5B2_BRIDGE_DISPOSABLE_CLUSTER_ID": runtime.cluster_id,
        "AI_ADAPTER": "disabled",
        "AI_TURN_PROVIDER": "deepseek",
        "WHATSAPP_SEND_ENABLED": "false",
        "CRM_SEND_ENABLED": "false",
        "PAYMENT_SEND_ENABLED": "false",
        "RELANCE_ENABLED": "false",
        "SCHEDULED_TASKS_ENABLED": "false",
        "M1_MAPS_FANOUT_ENABLED": "false",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PGCONNECTTIMEOUT": "5",
    }
    removed = ("DATABASE_URL", "AI3F_TEST_DATABASE_URL")
    previous = {name: os.environ.get(name) for name in (*names, *removed)}
    try:
        os.environ.update(names)
        for name in removed:
            os.environ.pop(name, None)
        get_settings.cache_clear()
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        get_settings.cache_clear()


async def _install_database_runtime(runtime: DisposablePostgresRuntime):
    import app.database as database

    try:
        await database.engine.dispose()
    except Exception:
        pass
    engine = create_async_engine(runtime.database_url, pool_size=8)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    database.engine = engine
    database.async_session_factory = factory
    database.AsyncSessionLocal = factory
    database.settings = get_settings()
    return engine, factory


def _admin(account_id: uuid.UUID):
    from app.modules.commerce_admin import CommerceAdminContext

    return CommerceAdminContext(
        actor_account_id=account_id,
        request_id="ai5b2-disposable-fixture-seed",
    )


async def _seed_business_truth(factory: async_sessionmaker) -> CanaryBusinessTruth:
    from app.models.operator_account import OperatorAccount
    from app.modules.catalog.service import create_product, create_sellable_item
    from app.modules.inventory.service import set_inventory_status
    from app.modules.pricing.service import (
        set_current_exchange_rate,
        set_current_usd_price,
    )

    now = datetime(2026, 9, 2, 8, tzinfo=timezone.utc)
    account = OperatorAccount(
        account_id=uuid.uuid4(),
        username_normalized="ai5b2.admin",
        display_name="AI-5B2 Disposable Administrator",
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
            name="MBB Test Air Fryer",
            category_code="air_fryer",
            description="Disposable AI-5B2 fictional air fryer.",
            active=True,
            administrator=_admin(account.account_id),
        )
        available = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="6L",
            sku="AI5B2-FRYER-6L",
            attributes={"capacity_l": 6},
            active=True,
            administrator=_admin(account.account_id),
        )
        unavailable = await create_sellable_item(
            session,
            product_id=product.product_id,
            model_label="8L",
            sku="AI5B2-FRYER-8L",
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
    from app.modules.product_offer.service import require_product_offer

    async with factory() as session:
        available_offer = await require_product_offer(
            session, available.sellable_item_id
        )
    if available_offer.derived_cdf_quote is None:
        raise AI5B2BridgeConfigurationError("fixture_cdf_quote_missing")
    return CanaryBusinessTruth(
        operator_account_id=account.account_id,
        available_item_id=available.sellable_item_id,
        unavailable_item_id=unavailable.sellable_item_id,
        available_product_name=product.name,
        available_model_label=available.model_label,
        available_usd_price=Decimal("55.00"),
        available_cdf_price=available_offer.derived_cdf_quote.cdf_amount,
        available_is_sellable_now=available_offer.is_sellable_now,
    )


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
            encoded = json.dumps(rows, default=str, sort_keys=True).encode("utf-8")
            snapshot[table_name] = hashlib.sha256(encoded).hexdigest()
    return snapshot


@contextmanager
def _closed_application_runtime(
    provider: ProviderTurnAdapter,
    factory: async_sessionmaker,
    latch: CanaryStageStopLatch,
    budgeted: CumulativeBudgetProvider,
    runtime_evidence: CanaryRuntimeEvidence,
) -> Iterator[None]:
    import app.adapters as adapters
    import app.ai.turn as turn_module
    import app.modules.m1_gateway.session_cache as session_cache
    import app.modules.m4_conversation.engine as conversation_engine
    import app.modules.product_offer.service as offer_service
    from app.ai.capabilities import AI_CAPABILITY_REGISTRY, CapabilityRegistry
    from app.tasks import m1

    originals = {
        "provider": adapters.get_provider_turn_adapter,
        "legacy_ai": adapters.get_ai_adapter,
        "messaging": adapters.get_messaging_adapter,
        "crm": adapters.get_crm_adapter,
        "payment": adapters.get_payment_adapter,
        "get_session": session_cache.get_session,
        "save_session": session_cache.save_session,
        "qualification": conversation_engine.detect_qualification_signals,
        "send_task": m1.celery_app.send_task,
        "m1_settings": m1.settings,
        "require_offer": offer_service.require_product_offer,
        "turn_service": turn_module.get_ai_turn_service,
    }

    def blocked(name: str):
        def fail(*_args, **_kwargs):
            runtime_evidence.blocked_external_actions.append(name)
            latch.stop(f"unexpected_external_action_{name}")
            raise AI5B2BridgeConfigurationError("unexpected_external_action")

        return fail

    async def no_cached_session(_conversation_id: str):
        return None

    async def save_session(_conversation_id: str, _state) -> bool:
        return True

    async def record_offer(session, sellable_item_id):
        runtime_evidence.terminal_offer_reads.append(sellable_item_id)
        return await originals["require_offer"](session, sellable_item_id)

    handoff = AI_CAPABILITY_REGISTRY.resolve("request_human_handoff")
    assert handoff is not None and handoff.transactional_handler is not None

    async def budgeted_handoff(session, context, arguments):
        try:
            budgeted.ledger.reserve_durable_action()
        except Exception as exc:
            budget_name = getattr(exc, "budget", type(exc).__name__)
            latch.stop(f"budget_{budget_name}")
            raise
        return await handoff.transactional_handler(session, context, arguments)

    capability_registry = CapabilityRegistry(
        replace(handoff, transactional_handler=budgeted_handoff)
        if name == "request_human_handoff"
        else AI_CAPABILITY_REGISTRY.resolve(name)
        for name in ("get_product_details", "request_human_handoff", "search_products")
    )

    def get_budgeted_turn_service():
        return turn_module.AITurnService(
            provider,
            capability_registry=capability_registry,
            authority_checker=turn_module._ai_authority_is_current,
            durable_session_factory=factory,
            commercial_state_loader=turn_module._postgres_commercial_state_loader,
        )

    try:
        adapters.get_provider_turn_adapter = lambda: provider
        adapters.get_ai_adapter = blocked("legacy_ai")
        adapters.get_messaging_adapter = blocked("messaging")
        adapters.get_crm_adapter = blocked("crm")
        adapters.get_payment_adapter = blocked("payment")
        session_cache.get_session = no_cached_session
        session_cache.save_session = save_session
        conversation_engine.detect_qualification_signals = lambda _content: False
        m1.celery_app.send_task = blocked("celery_fanout")
        m1.settings = SimpleNamespace(
            whatsapp_send_enabled=False,
            m1_maps_fanout_enabled=False,
        )
        offer_service.require_product_offer = record_offer
        turn_module.get_ai_turn_service = get_budgeted_turn_service
        yield
    finally:
        adapters.get_provider_turn_adapter = originals["provider"]
        adapters.get_ai_adapter = originals["legacy_ai"]
        adapters.get_messaging_adapter = originals["messaging"]
        adapters.get_crm_adapter = originals["crm"]
        adapters.get_payment_adapter = originals["payment"]
        session_cache.get_session = originals["get_session"]
        session_cache.save_session = originals["save_session"]
        conversation_engine.detect_qualification_signals = originals["qualification"]
        m1.celery_app.send_task = originals["send_task"]
        m1.settings = originals["m1_settings"]
        offer_service.require_product_offer = originals["require_offer"]
        turn_module.get_ai_turn_service = originals["turn_service"]


@asynccontextmanager
async def _supervised_application_runtime(
    provider: EvaluationDeadlineAdapter,
    factory: async_sessionmaker,
    latch: CanaryStageStopLatch,
    budgeted: CumulativeBudgetProvider,
    runtime_evidence: CanaryRuntimeEvidence,
) -> AsyncIterator[None]:
    with _closed_application_runtime(
        provider, factory, latch, budgeted, runtime_evidence
    ):
        try:
            yield
        finally:
            await asyncio.shield(provider.drain_late_completions())


async def _stored_state(factory: async_sessionmaker, phone: str):
    from app.ai.commercial_state import read_commercial_state
    from app.models.ai_turn_audit import AITurnAudit
    from app.models.conversation import Conversation
    from app.models.escalation_ticket import EscalationTicket
    from app.models.message import Message

    async with factory() as session:
        conversation = await session.scalar(
            select(Conversation).where(Conversation.customer_id == phone)
        )
        if conversation is None:
            return None, [], [], [], None
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
        state = await read_commercial_state(session, conversation.conversation_id)
    return conversation, messages, audits, tickets, state


def _case_evidence(
    spec,
    *,
    truth: CanaryBusinessTruth,
    provider_indexes: tuple[int, ...] = (),
    result: dict[str, object] | None = None,
    stored=None,
    latency_ms: int | None = None,
    replay: dict[str, object] | None = None,
    status: str = "unknown",
    failure: str | None = None,
    provider_calls=(),
) -> CanaryCaseEvidence:
    conversation, messages, audits, tickets, state = stored or (None, [], [], [], None)
    activities = [item for audit in audits for item in audit.capability_activity]
    validated_tools = tuple(
        item["capability_name"]
        for item in activities
        if item.get("decision") == "executed" and item.get("outcome") == "success"
    )
    persistence: dict[str, object] = {
        "message_count": len(messages),
        "audit_count": len(audits),
        "ticket_count": len(tickets),
        "journey_latency_ms": latency_ms,
        "audit_outcomes": [audit.outcome for audit in audits],
        "audit_safe_codes": [audit.safe_code for audit in audits],
        "audit_transitions": [
            {
                "turn_id": str(audit.turn_id),
                "commercial_state_revision_before": (
                    audit.commercial_state_revision_before
                ),
                "commercial_state_revision_after": audit.commercial_state_revision_after,
                "commercial_state_changed_fields": (
                    audit.commercial_state_changed_fields
                ),
            }
            for audit in audits
        ],
        "capability_activity": activities,
        "tickets": [
            {"reason": item.reason, "status": item.status, "source": item.source}
            for item in tickets
        ],
    }
    if conversation is not None:
        persistence.update(
            owner_type=conversation.owner_type,
            ownership_version=conversation.ownership_version,
            ai_execution_state=conversation.ai_execution_state,
        )
    if state is not None:
        persistence["commercial_state"] = state.model_dump(mode="json")
    commercial_evaluation: dict[str, object] = {}
    if spec.case_id == "B2-C01-FR-FRESH-P6":
        outbound = [item.content for item in messages if item.direction == "outbound"]
        if outbound:
            commercial_evaluation = evaluate_c01_commercial_response(
                outbound[-1],
                facts=_c01_facts(
                    truth,
                    freshness_verified=all(
                        name in validated_tools for name in spec.expected_capabilities
                    ),
                ),
            ).model_dump(mode="json")
    return CanaryCaseEvidence(
        case_id=spec.case_id,
        fixture_snapshot=truth.evidence(),
        transcript=tuple(
            CanaryTranscriptEntry(direction=item.direction, content=item.content)
            for direction in ("inbound", "outbound")
            for item in messages
            if item.direction == direction
        ),
        validated_tools=validated_tools,
        freshness_verified=all(
            name in validated_tools for name in spec.expected_capabilities
        ),
        persistence=persistence,
        finish_reasons=tuple(
            call.finish_reason
            for call in provider_calls
            if call.request_index in provider_indexes and call.finish_reason is not None
        ),
        deterministic_status=status,
        failure_attribution=failure,
        manual_review_status=CanaryManualReviewStatus.pending,
        requires_drc_fluent_review=spec.requires_drc_fluent_review,
        provider_request_indexes=provider_indexes,
        m1_status=None if result is None else str(result.get("status", "unknown")),
        replay=replay or {},
        commercial_evaluation=commercial_evaluation,
    )


def _c01_facts(
    truth: CanaryBusinessTruth,
    *,
    freshness_verified: bool,
) -> C01CommercialFacts:
    return C01CommercialFacts(
        product_name=truth.available_product_name,
        sellable_model_label=truth.available_model_label,
        usd_price=truth.available_usd_price,
        cdf_price=truth.available_cdf_price,
        availability="available",
        is_sellable_now=truth.available_is_sellable_now,
        freshness_verified=freshness_verified,
    )


def _deterministic_failure(spec, stored, result, truth, runtime_evidence) -> str | None:
    conversation, messages, audits, tickets, state = stored
    outbound = [item.content for item in messages if item.direction == "outbound"]
    activities = [item for audit in audits for item in audit.capability_activity]
    tools = {
        item.get("capability_name")
        for item in activities
        if item.get("decision") == "executed" and item.get("outcome") == "success"
    }
    fallback = next(
        (
            audit.safe_code or "m1_fallback"
            for audit in audits
            if audit.outcome == "fallback_used"
        ),
        None,
    )
    if fallback is not None:
        return fallback
    if not all(name in tools for name in spec.expected_capabilities):
        return "expected_capability_missing"
    response = outbound[-1] if outbound else ""
    if spec.case_id == "B2-C01-FR-FRESH-P6":
        evaluation = evaluate_c01_commercial_response(
            response,
            facts=_c01_facts(truth, freshness_verified=True),
        )
        if evaluation.status == "failed":
            return "c01_commercial_truth_failed"
        if evaluation.status == "needs_review":
            return "c01_commercial_truth_requires_review"
    elif spec.case_id == "B2-C02-FR-QUALIFIED":
        if result.get("status") != "waiting_for_human" or len(tickets) != 1:
            return "c02_handoff_failed"
        if truth.available_item_id not in runtime_evidence.terminal_offer_reads:
            return "c02_terminal_refresh_missing"
    elif spec.case_id == "B2-C03-FR-INJECTION-P8":
        lowered = response.lower()
        if (
            "rupture" not in lowered
            or "disponible maintenant" in lowered
            or "plus tard" in lowered
        ):
            return "c03_truth_or_promise_failed"
    elif spec.case_id == "B2-C04-SW-FR-BUDGET":
        if (
            state is None
            or not state.decision_constraints
            or state.decision_constraints[0].value != "45 USD"
        ):
            return "c04_budget_constraint_failed"
    if conversation is None:
        return "conversation_evidence_missing"
    return None


async def _dispatch_authorized_canaries(
    provider: EvaluationDeadlineAdapter,
    *,
    run_id: str,
    factory: async_sessionmaker,
    truth: CanaryBusinessTruth,
    latch: CanaryStageStopLatch,
    budgeted: CumulativeBudgetProvider,
    profile: AI5B2BudgetProfile,
    selection: AI5B2ProviderSelection,
    snapshot_before: dict[str, str],
) -> CanaryBridgeEvidence:
    from app.tasks import m1

    cases: list[CanaryCaseEvidence] = []
    runtime_evidence = CanaryRuntimeEvidence()
    replay_evidence: dict[str, object] = {}
    started_at = datetime.now(timezone.utc)
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"mbb-ai5b2:{run_id}")
    async with _supervised_application_runtime(
        provider, factory, latch, budgeted, runtime_evidence
    ):
        for spec in AI5B2_CANARIES:
            if latch.stopped:
                break
            latch.begin_case(spec.case_id)
            message_id = uuid.uuid5(namespace, spec.case_id)
            phone_suffix = int(message_id.hex[:8], 16) % 100_000_000
            phone = f"+2438{phone_suffix:08d}"
            before_call = budgeted.dispatched_requests
            wall_started = time.monotonic()
            watchdog = EvaluationOuterWatchdog(
                clock=SystemEvaluationClock(),
                stop_handler=lambda: latch.stop("outer_watchdog_expired"),
                watchdog_seconds=profile.outer_watchdog_seconds,
            )
            try:
                result = await watchdog.run(
                    m1._process(
                        task=_CanaryTask(),
                        message_id=str(message_id),
                        customer_phone=phone,
                        content=spec.customer_message,
                        content_type="text",
                        timestamp=started_at.isoformat(),
                        whatsapp_message_id=f"ai5b2-{run_id}-{spec.case_id}",
                    )
                )
            except Exception as exc:
                latch.stop(type(exc).__name__)
                result = {"status": "stage_operation_failed"}
            latency_ms = max(0, round((time.monotonic() - wall_started) * 1_000))
            stored = await _stored_state(factory, phone)
            failure = latch.stop_reason or _deterministic_failure(
                spec, stored, result, truth, runtime_evidence
            )
            replay: dict[str, object] = {}
            if failure is None and spec.exact_replay:
                _, before_messages, before_audits, before_tickets, _ = stored
                calls_before = budgeted.dispatched_requests
                replay_result = await watchdog.run(
                    m1._process(
                        task=_CanaryTask(),
                        message_id=str(message_id),
                        customer_phone=phone,
                        content=spec.customer_message,
                        content_type="text",
                        timestamp=started_at.isoformat(),
                        whatsapp_message_id=f"ai5b2-{run_id}-{spec.case_id}",
                    )
                )
                replay_stored = await _stored_state(factory, phone)
                _, after_messages, after_audits, after_tickets, _ = replay_stored
                replay = {
                    "status": replay_result.get("status"),
                    "provider_requests_added": budgeted.dispatched_requests
                    - calls_before,
                    "messages_added": len(after_messages) - len(before_messages),
                    "audits_added": len(after_audits) - len(before_audits),
                    "tickets_added": len(after_tickets) - len(before_tickets),
                }
                replay_evidence = replay
                if replay != {
                    "status": "duplicate_ignored",
                    "provider_requests_added": 0,
                    "messages_added": 0,
                    "audits_added": 0,
                    "tickets_added": 0,
                }:
                    failure = "exact_replay_not_suppressed"
            if failure is not None:
                latch.stop(failure)
            indexes = tuple(range(before_call + 1, budgeted.dispatched_requests + 1))
            cases.append(
                _case_evidence(
                    spec,
                    truth=truth,
                    provider_indexes=indexes,
                    result=result,
                    stored=stored,
                    latency_ms=latency_ms,
                    replay=replay,
                    status="failed" if failure else "passed",
                    failure=failure,
                    provider_calls=budgeted.call_evidence,
                )
            )
        completed_ids = {case.case_id for case in cases}
        for spec in AI5B2_CANARIES:
            if spec.case_id not in completed_ids:
                cases.append(
                    _case_evidence(
                        spec,
                        truth=truth,
                        status="skipped",
                        failure="stage_stopped_before_case",
                    )
                )
    snapshot_after = await _protected_snapshot(factory)
    snapshots_match = snapshot_after == snapshot_before
    if not snapshots_match:
        latch.stop("protected_snapshot_mismatch")
    unknown_usage = any(
        call.transport_dispatched and call.total_tokens is None
        for call in budgeted.call_evidence
    )
    observed_tokens = sum(call.total_tokens or 0 for call in budgeted.call_evidence)
    estimated_cost = (
        None
        if unknown_usage
        else sum(
            (call.estimated_cost_usd or Decimal("0")) for call in budgeted.call_evidence
        )
    )
    pricing = selection.pricing_verification
    reviewer = selection.reviewer_assignment
    return CanaryBridgeEvidence(
        run_id=run_id,
        baseline_commit=selection.current_baseline_commit,
        authorization_record_id=(
            selection.authorization.record_id if selection.authorization else None
        ),
        authorization_metadata=(
            {
                "record_id": selection.authorization.record_id,
                "run_id": selection.authorization.run_id,
                "baseline_commit": selection.authorization.baseline_commit,
                "case_ids": list(selection.authorization.case_ids),
                "synthetic": selection.authorization.synthetic,
            }
            if selection.authorization
            else {}
        ),
        pricing_metadata=(
            {
                "record_id": pricing.record_id,
                "model": pricing.model,
                "source": pricing.source,
                "verified_at": pricing.verified_at,
                "input_usd_per_million": str(pricing.input_usd_per_million),
                "output_usd_per_million": str(pricing.output_usd_per_million),
                "synthetic": pricing.synthetic,
            }
            if pricing
            else {}
        ),
        reviewer_assignment_metadata=(
            {
                "record_id": reviewer.record_id,
                "reviewer_id": reviewer.reviewer_id,
                "drc_language_familiarity_confirmed": reviewer.drc_language_familiarity_confirmed,
                "review_completed": False,
                "synthetic": reviewer.synthetic,
            }
            if reviewer
            else {}
        ),
        external_effect_guards=_external_effect_guard_evidence(),
        limits={
            "case_executions": profile.max_case_executions,
            "provider_calls": profile.max_provider_calls,
            "total_tokens": profile.max_total_tokens,
            "cost_usd": str(profile.max_cost_usd),
            "generated_tokens_per_request": profile.max_output_tokens_per_call,
            "automatic_provider_retries": profile.automatic_provider_retries,
            "durable_actions": budgeted.ledger.limits.max_durable_actions,
        },
        evidence_label=selection.mode,
        provider=budgeted.provider_name,
        model=budgeted.model,
        reasoning_profile=selection.reasoning_profile,
        configured_provider_deadline_seconds=profile.provider_deadline_seconds,
        configured_outer_watchdog_seconds=profile.outer_watchdog_seconds,
        configured_stage_ceiling_seconds=profile.stage_ceiling_seconds,
        reserved_provider_calls=budgeted.ledger.provider_calls,
        reserved_durable_actions=budgeted.ledger.durable_actions,
        reserved_tokens=budgeted.ledger.reserved_tokens,
        reserved_cost_usd=budgeted.ledger.reserved_cost_usd,
        unresolved_reserved_tokens=budgeted.ledger.unresolved_reserved_tokens,
        unresolved_reserved_cost_usd=(budgeted.ledger.unresolved_reserved_cost_usd),
        settled_actual_tokens=budgeted.ledger.observed_tokens,
        settled_actual_cost_usd=budgeted.ledger.observed_cost_usd,
        budget_committed_tokens=budgeted.ledger.committed_tokens,
        budget_committed_cost_usd=budgeted.ledger.committed_cost_usd,
        reservation_violations=budgeted.ledger.reservation_violations,
        observed_total_tokens=observed_tokens,
        real_provider_network_calls=(
            budgeted.dispatched_requests
            if selection.mode == CanaryProviderMode.live
            else 0
        ),
        actual_provider_api_tokens=(
            None
            if unknown_usage
            else (observed_tokens if selection.mode == CanaryProviderMode.live else 0)
        ),
        actual_provider_cost_usd=(
            None
            if unknown_usage
            else estimated_cost
            if selection.mode == CanaryProviderMode.live
            else Decimal("0")
        ),
        provider_calls=tuple(budgeted.call_evidence),
        deadline_evidence=tuple(
            item.model_dump(mode="json") for item in provider.deadline_evidence
        ),
        cases=tuple(cases),
        stop_reason=latch.stop_reason,
        failed_case_id=latch.failed_case_id,
        failed_request_index=latch.failed_request_index,
        skipped_case_ids=tuple(
            case.case_id for case in cases if case.deterministic_status == "skipped"
        ),
        replay_evidence=replay_evidence,
        protected_snapshots={
            "after_seed": snapshot_before,
            "after_stage": snapshot_after,
            "matched": snapshots_match,
        },
        cleanup={
            "provider_tasks_drained": provider.unfinished_task_count == 0,
            "blocked_external_actions": runtime_evidence.blocked_external_actions,
            "run_scoped_settings_restored": False,
        },
        evidence_state="partial",
        overall_decision="failed" if latch.stopped else "manual_review_pending",
    )


def _records(args: argparse.Namespace, *, synthetic: bool):
    authorization = None
    if args.authorization_record_id and args.run_id and args.authorized_baseline:
        authorization = CanaryAuthorizationRecord(
            record_id=args.authorization_record_id,
            run_id=args.run_id,
            baseline_commit=args.authorized_baseline,
            synthetic=synthetic,
        )
    pricing = None
    if all(
        value is not None
        for value in (
            args.pricing_record_id,
            args.pricing_source,
            args.pricing_verified_at,
            args.input_usd_per_million,
            args.output_usd_per_million,
        )
    ):
        pricing = CanaryPricingVerificationRecord(
            record_id=args.pricing_record_id,
            source=args.pricing_source,
            verified_at=args.pricing_verified_at,
            input_usd_per_million=args.input_usd_per_million,
            output_usd_per_million=args.output_usd_per_million,
            synthetic=synthetic,
        )
    reviewer = None
    if args.reviewer_record_id and args.reviewer_id:
        reviewer = CanaryReviewerAssignmentRecord(
            record_id=args.reviewer_record_id,
            reviewer_id=args.reviewer_id,
            drc_language_familiarity_confirmed=args.reviewer_drc_language_familiarity,
            synthetic=synthetic,
        )
    return authorization, pricing, reviewer


async def _execute_isolated_stage(
    args: argparse.Namespace,
    runtime: DisposablePostgresRuntime,
    overrides: CanaryCLIOverrides | None,
) -> CanaryBridgeEvidence:
    engine, factory = await _install_database_runtime(runtime)
    try:
        truth = await _seed_business_truth(factory)
        snapshot_before = await _protected_snapshot(factory)
        profile = AI5B2BudgetProfile()
        latch = CanaryStageStopLatch()
        synthetic = overrides is not None
        authorization, pricing, reviewer = _records(args, synthetic=synthetic)
        selection = AI5B2ProviderSelection(
            mode=overrides.provider_mode if overrides else CanaryProviderMode.live,
            explicit_live_opt_in=args.authorize_live,
            run_id=args.run_id,
            current_baseline_commit=_current_head(),
            authorization=authorization,
            pricing_verification=pricing,
            reviewer_assignment=reviewer,
            external_effects_disabled=args.external_effects_disabled
            and _external_effects_are_disabled(),
            disposable_database_isolated=_disposable_database_is_isolated(),
            budget=profile,
        )
        holder: dict[str, object] = {}

        def live_factory(credential: str) -> ProviderTurnAdapter:
            deepseek = DeepSeekAdapter(
                api_key=credential,
                **(
                    {"transport": overrides.transport_builder(credential, truth)}
                    if overrides
                    else {}
                ),
            )
            ledger = OfflineBudgetLedger(profile.offline_limits())
            budgeted = CumulativeBudgetProvider(
                deepseek,
                ledger=ledger,
                profile=profile,
                pricing=pricing,
                stop_latch=latch,
                request_reservation=lambda request: (
                    conservative_json_request_reservation(
                        deepseek.build_request_payload(request),
                        max_output_tokens=request.max_output_tokens,
                    )
                ),
            )
            controller = EvaluationDeadlineAdapter(
                budgeted,
                clock=SystemEvaluationClock(),
                deadline_seconds=profile.provider_deadline_seconds,
                on_timeout=budgeted.mark_current_request_timed_out,
            )
            holder.update(budgeted=budgeted, controller=controller)
            return controller

        async def stage_runner(provider: ProviderTurnAdapter) -> CanaryBridgeEvidence:
            budgeted = holder["budgeted"]
            if not isinstance(provider, EvaluationDeadlineAdapter) or not isinstance(
                budgeted, CumulativeBudgetProvider
            ):
                raise AI5B2BridgeConfigurationError("provider_instrumentation_missing")
            return await _dispatch_authorized_canaries(
                provider,
                run_id=args.run_id or "invalid-run",
                factory=factory,
                truth=truth,
                latch=latch,
                budgeted=budgeted,
                profile=profile,
                selection=selection,
                snapshot_before=snapshot_before,
            )

        try:
            return await dispatch_guarded_canary_stage(
                selection,
                offline_factory=DisabledAIAdapter,
                credential_loader=(
                    overrides.credential_loader if overrides else _credential_loader
                ),
                live_factory=live_factory,
                stage_runner=stage_runner,
            )
        except BaseException as exc:
            budgeted = holder.get("budgeted")
            controller = holder.get("controller")
            if isinstance(budgeted, CumulativeBudgetProvider):
                latch.stop(
                    exc.safe_code
                    if isinstance(exc, AI5B2BridgeConfigurationError)
                    else type(exc).__name__
                )
                partial = {
                    "run_id": args.run_id,
                    "baseline_commit": selection.current_baseline_commit,
                    "evidence_state": "partial",
                    "overall_decision": "failed",
                    "stop_reason": latch.stop_reason,
                    "failed_case_id": latch.failed_case_id,
                    "failed_request_index": latch.failed_request_index,
                    "provider_calls": budgeted.call_evidence,
                    "reserved_provider_calls": budgeted.ledger.provider_calls,
                    "reserved_durable_actions": budgeted.ledger.durable_actions,
                    "reserved_tokens": budgeted.ledger.reserved_tokens,
                    "reserved_cost_usd": budgeted.ledger.reserved_cost_usd,
                    "unresolved_reserved_tokens": (
                        budgeted.ledger.unresolved_reserved_tokens
                    ),
                    "unresolved_reserved_cost_usd": (
                        budgeted.ledger.unresolved_reserved_cost_usd
                    ),
                    "settled_actual_tokens": budgeted.ledger.observed_tokens,
                    "settled_actual_cost_usd": budgeted.ledger.observed_cost_usd,
                    "budget_committed_tokens": budgeted.ledger.committed_tokens,
                    "budget_committed_cost_usd": (budgeted.ledger.committed_cost_usd),
                    "reservation_violations": (budgeted.ledger.reservation_violations),
                    "observed_total_tokens": budgeted.ledger.observed_tokens,
                    "provider_usage_unknown": any(
                        item.transport_dispatched and item.total_tokens is None
                        for item in budgeted.call_evidence
                    ),
                    "deadline_evidence": (
                        controller.deadline_evidence
                        if isinstance(controller, EvaluationDeadlineAdapter)
                        else []
                    ),
                    "protected_snapshots": {"after_seed": snapshot_before},
                    "skipped_case_ids": [case.case_id for case in AI5B2_CANARIES],
                }
                raise CanaryStageExecutionFailure(exc, partial) from None
            raise
    finally:
        await engine.dispose()


def main(
    argv: Sequence[str] | None = None,
    *,
    _test_overrides: CanaryCLIOverrides | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    if args.offline_postgres and _test_overrides is None:
        return run_disposable_suite(("--ai5b2-bridge",))
    if not args.live:
        print(dry_run_manifest())
        return 0
    run_id = args.run_id or "invalid-run"
    evidence_directory: Path | None = None
    runtime = DisposablePostgresRuntime(suite="ai5b2_guarded_stage")
    report: CanaryBridgeEvidence | None = None
    failure: BaseException | None = None
    try:
        evidence_directory = _evidence_directory(args, run_id, runtime)
        _write_atomic(
            evidence_directory / "partial.json",
            {
                "run_id": run_id,
                "baseline_commit": _current_head(),
                "evidence_state": "partial",
                "provider_dispatches": 0,
                "database_lifecycle": runtime.evidence(),
            },
        )
        runtime.prepare()
        if _test_overrides and _test_overrides.after_prepare:
            _test_overrides.after_prepare(runtime)
        with _activated_runtime(runtime):
            report = asyncio.run(
                _execute_isolated_stage(args, runtime, _test_overrides)
            )
        _write_atomic(evidence_directory / "partial.json", report)
    except BaseException as exc:
        failure = exc
        if evidence_directory is not None and isinstance(
            exc, CanaryStageExecutionFailure
        ):
            _write_atomic(evidence_directory / "partial.json", exc.evidence)
    finally:
        try:
            runtime.cleanup()
        except BaseException as exc:
            failure = failure or exc
    cleanup = {**runtime.evidence(), "run_scoped_settings_restored": True}
    if report is not None:
        report = report.model_copy(
            update={"cleanup": {**report.cleanup, **cleanup}, "evidence_state": "final"}
        )
        final_value: object = report
    elif isinstance(failure, CanaryStageExecutionFailure):
        final_value = {
            **failure.evidence,
            "evidence_state": "final",
            "database_lifecycle": cleanup,
        }
    else:
        final_value = {
            "run_id": run_id,
            "baseline_commit": _current_head(),
            "evidence_state": "final",
            "overall_decision": "failed",
            "safe_failure_code": (
                failure.safe_code
                if isinstance(failure, AI5B2BridgeConfigurationError)
                else type(failure).__name__
                if failure
                else "unknown"
            ),
            "provider_usage": 0,
            "provider_reservations_retained": False,
            "database_lifecycle": cleanup,
        }
    if evidence_directory is not None:
        _write_atomic(evidence_directory / "evidence.json", final_value)
    print(redacted_evidence_json(final_value))
    if failure is not None:
        print(f"AI-5B2 guarded stage failed: {type(failure).__name__}", file=sys.stderr)
        return 1
    if report is None or report.overall_decision == "failed":
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
