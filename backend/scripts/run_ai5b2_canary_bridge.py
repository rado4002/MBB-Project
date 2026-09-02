"""Dry-run or offline-validate the AI-5B2 real-journey bridge."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy.engine import make_url

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.ai.deepseek_adapter import DeepSeekAdapter  # noqa: E402
from app.adapters.ai.disabled_adapter import DisabledAIAdapter  # noqa: E402
from app.adapters.base import ProviderTurnAdapter  # noqa: E402
from app.ai.canary_bridge import (  # noqa: E402
    AI5B2BridgeConfigurationError,
    AI5B2BudgetProfile,
    AI5B2ProviderSelection,
    CanaryAuthorizationRecord,
    CanaryPricingVerificationRecord,
    CanaryProviderMode,
    CanaryReviewerAssignmentRecord,
    CumulativeBudgetProvider,
    dispatch_guarded_canary_stage,
    dry_run_manifest,
)
from app.ai.offline_certification import (  # noqa: E402
    EvaluationDeadlineAdapter,
    EvaluationOuterWatchdog,
    OfflineBudgetLedger,
    SystemEvaluationClock,
)
from app.config import get_settings  # noqa: E402
from scripts.run_ai5b1_offline_certification import (  # noqa: E402
    main as run_disposable_suite,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--offline-postgres",
        action="store_true",
        help="run the bridge checks in a new disposable PostgreSQL cluster",
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


async def _dispatch_authorized_canaries(
    provider: ProviderTurnAdapter,
    *,
    run_id: str,
    watchdog_seconds: int,
) -> dict[str, object]:
    """Execute the four frozen M1 inbounds in an already-isolated evaluation DB."""
    import app.adapters as adapters
    from app.ai.canary_bridge import AI5B2_CANARIES
    from app.tasks import m1

    original_factory = adapters.get_provider_turn_adapter
    adapters.get_provider_turn_adapter = lambda: provider
    results: list[dict] = []
    started = datetime.now(timezone.utc)
    run_namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"mbb-ai5b2:{run_id}")
    try:
        for index, case in enumerate(AI5B2_CANARIES):
            message_id = uuid.uuid5(run_namespace, case.case_id)
            phone_suffix = int(message_id.hex[:8], 16) % 100_000_000
            watchdog = EvaluationOuterWatchdog(
                clock=SystemEvaluationClock(),
                stop_handler=lambda: None,
                watchdog_seconds=watchdog_seconds,
            )
            result = await watchdog.run(
                m1._process(
                    task=_CanaryTask(),
                    message_id=str(message_id),
                    customer_phone=f"+2438{phone_suffix:08d}",
                    content=case.customer_message,
                    content_type="text",
                    timestamp=started.isoformat(),
                    whatsapp_message_id=f"ai5b2-{run_id}-{case.case_id}",
                )
            )
            results.append(result)
            if case.exact_replay:
                replay = await watchdog.run(
                    m1._process(
                        task=_CanaryTask(),
                        message_id=str(message_id),
                        customer_phone=f"+2438{phone_suffix:08d}",
                        content=case.customer_message,
                        content_type="text",
                        timestamp=started.isoformat(),
                        whatsapp_message_id=f"ai5b2-{run_id}-{case.case_id}",
                    )
                )
                if replay.get("status") != "duplicate_ignored":
                    raise AI5B2BridgeConfigurationError("exact_replay_not_suppressed")
    finally:
        adapters.get_provider_turn_adapter = original_factory
        if isinstance(provider, EvaluationDeadlineAdapter):
            await provider.drain_late_completions()
    return {
        "case_ids": [case.case_id for case in AI5B2_CANARIES],
        "statuses": [result.get("status", "unknown") for result in results],
        "manual_review_status": "pending",
        "live_provider_requests_are_not_reported_as_offline": True,
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.offline_postgres:
        return run_disposable_suite(("--ai5b2-bridge",))
    if not args.live:
        print(dry_run_manifest())
        return 0

    budget = AI5B2BudgetProfile()
    current_head = _current_head()
    authorization = None
    if args.authorization_record_id and args.run_id and args.authorized_baseline:
        authorization = CanaryAuthorizationRecord(
            record_id=args.authorization_record_id,
            run_id=args.run_id,
            baseline_commit=args.authorized_baseline,
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
        )
    reviewer = None
    if args.reviewer_record_id and args.reviewer_id:
        reviewer = CanaryReviewerAssignmentRecord(
            record_id=args.reviewer_record_id,
            reviewer_id=args.reviewer_id,
            drc_language_familiarity_confirmed=(args.reviewer_drc_language_familiarity),
        )
    selection = AI5B2ProviderSelection(
        mode=CanaryProviderMode.live,
        explicit_live_opt_in=args.authorize_live,
        run_id=args.run_id,
        current_baseline_commit=current_head,
        authorization=authorization,
        pricing_verification=pricing,
        reviewer_assignment=reviewer,
        external_effects_disabled=(
            args.external_effects_disabled and _external_effects_are_disabled()
        ),
        disposable_database_isolated=_disposable_database_is_isolated(),
        budget=budget,
    )

    def live_factory(credential: str) -> ProviderTurnAdapter:
        ledger = OfflineBudgetLedger(budget.offline_limits())
        budgeted = CumulativeBudgetProvider(
            DeepSeekAdapter(api_key=credential),
            ledger=ledger,
            profile=budget,
            pricing=pricing,
        )
        return EvaluationDeadlineAdapter(
            budgeted,
            clock=SystemEvaluationClock(),
            deadline_seconds=budget.provider_deadline_seconds,
        )

    try:
        result = asyncio.run(
            dispatch_guarded_canary_stage(
                selection,
                offline_factory=DisabledAIAdapter,
                credential_loader=_credential_loader,
                live_factory=live_factory,
                stage_runner=lambda provider: _dispatch_authorized_canaries(
                    provider,
                    run_id=args.run_id or "invalid-run",
                    watchdog_seconds=budget.outer_watchdog_seconds,
                ),
            )
        )
    except AI5B2BridgeConfigurationError as exc:
        print(f"AI-5B2 bridge preflight failed: {exc.safe_code}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(
            f"AI-5B2 guarded stage failed: {type(exc).__name__}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
