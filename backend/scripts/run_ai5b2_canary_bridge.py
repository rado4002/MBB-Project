"""Dry-run or offline-validate the AI-5B2 real-journey bridge."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.ai.deepseek_adapter import DeepSeekAdapter  # noqa: E402
from app.adapters.ai.disabled_adapter import DisabledAIAdapter  # noqa: E402
from app.adapters.base import ProviderTurnAdapter  # noqa: E402
from app.ai.canary_bridge import (  # noqa: E402
    AI5B2BridgeConfigurationError,
    AI5B2BudgetProfile,
    AI5B2ProviderSelection,
    CanaryProviderMode,
    CumulativeBudgetProvider,
    dry_run_manifest,
    select_canary_provider,
)
from app.ai.offline_certification import (  # noqa: E402
    EvaluationDeadlineAdapter,
    OfflineBudgetLedger,
    SystemEvaluationClock,
)
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
        help="validate future live authorization only; never implied by credentials",
    )
    parser.add_argument("--authorize-live", action="store_true")
    parser.add_argument("--run-id")
    parser.add_argument("--pricing-verified", action="store_true")
    parser.add_argument("--manual-reviewer-assigned", action="store_true")
    parser.add_argument("--external-effects-disabled", action="store_true")
    return parser


def _credential_loader() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.offline_postgres:
        return run_disposable_suite(("--ai5b2-bridge",))
    if not args.live:
        print(dry_run_manifest())
        return 0

    budget = AI5B2BudgetProfile()
    selection = AI5B2ProviderSelection(
        mode=CanaryProviderMode.live,
        explicit_live_opt_in=args.authorize_live,
        run_id=args.run_id,
        pricing_verified=args.pricing_verified,
        manual_reviewer_assigned=args.manual_reviewer_assigned,
        external_effects_disabled=args.external_effects_disabled,
        budget=budget,
    )

    def live_factory(credential: str) -> ProviderTurnAdapter:
        ledger = OfflineBudgetLedger(budget.offline_limits())
        budgeted = CumulativeBudgetProvider(
            DeepSeekAdapter(api_key=credential),
            ledger=ledger,
            profile=budget,
        )
        return EvaluationDeadlineAdapter(
            budgeted,
            clock=SystemEvaluationClock(),
            deadline_seconds=budget.provider_deadline_seconds,
        )

    try:
        select_canary_provider(
            selection,
            offline_factory=DisabledAIAdapter,
            credential_loader=_credential_loader,
            live_factory=live_factory,
        )
    except AI5B2BridgeConfigurationError as exc:
        print(f"AI-5B2 bridge preflight failed: {exc.safe_code}", file=sys.stderr)
        return 1

    # Provider construction is only a preflight seam. Real journey dispatch remains
    # outside this offline-preparation task and requires separate authorization.
    print(
        "AI-5B2 live dispatch is not authorized by this offline bridge.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
