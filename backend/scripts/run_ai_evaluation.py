"""Run replay scoring or an explicitly activated isolated provider evaluation."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters import get_provider_turn_adapter  # noqa: E402
from app.adapters.base import ProviderTurnAdapter  # noqa: E402
from app.ai.evaluation import (  # noqa: E402
    EvaluationCase,
    EvaluationReplay,
    EvaluationRunner,
    ScriptedEvaluationSource,
)
from app.ai.evaluation_corpus import (  # noqa: E402
    get_mbb_evaluation_corpus,
)
from app.ai.live_evaluation import (  # noqa: E402
    LIVE_JOURNEY_CASE_IDS,
    LiveEvaluationBudgetExceeded,
    LiveEvaluationConfigurationError,
    LiveEvaluationFailureReport,
    LiveEvaluationProviderFailure,
    LiveEvaluationRunBudget,
    LiveJourneyController,
)
from app.ai.capabilities import SafeCapabilityError  # noqa: E402
from app.ai.journey_harness import (  # noqa: E402
    ProductionJourneyObservationSource,
    fixture_registry,
)
from app.ai.policy import AI_SYSTEM_POLICY_VERSION  # noqa: E402
from app.ai.provider_contract import (  # noqa: E402
    ProviderReasoningProfile,
)
from app.ai.turn import AITurnLimits, AITurnService  # noqa: E402
from app.config import Settings, get_settings  # noqa: E402

AdapterFactory = Callable[[], ProviderTurnAdapter]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--replay",
        type=Path,
        help="JSON replay containing run metadata and normalized observations.",
    )
    mode.add_argument(
        "--live",
        action="store_true",
        help="Explicitly activate the isolated provider evaluation source.",
    )
    parser.add_argument(
        "--profile",
        action="append",
        dest="profiles",
        choices=tuple(profile.value for profile in ProviderReasoningProfile),
        help="Reasoning profile for live mode; may be repeated.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Replay-only case filter; live mode uses the frozen canary IDs.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write JSON to this path instead of standard output.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Indent the machine-readable JSON report.",
    )
    return parser


async def _run(args: argparse.Namespace) -> str:
    if args.replay is not None:
        if args.profiles:
            raise LiveEvaluationConfigurationError("profile_requires_live_mode")
        return await _run_replay(args)
    if args.case_ids:
        raise LiveEvaluationConfigurationError("live_case_selection_is_frozen")
    return await _run_live(args)


async def _run_replay(args: argparse.Namespace) -> str:
    replay = EvaluationReplay.model_validate_json(
        args.replay.read_text(encoding="utf-8")
    )
    runner = EvaluationRunner(
        ScriptedEvaluationSource(replay.observations),
        replay.metadata,
    )
    report = await runner.run(
        get_mbb_evaluation_corpus(),
        case_ids=args.case_ids,
    )
    return _serialize_report(report, pretty=args.pretty)


async def _run_live(
    args: argparse.Namespace,
    *,
    configured_settings: Settings | None = None,
    adapter_factory: AdapterFactory | None = None,
    budget: LiveEvaluationRunBudget | None = None,
    environment: Mapping[str, str] | None = None,
) -> str:
    profiles = _profiles(args.profiles)
    settings = configured_settings or get_settings()
    _validate_live_settings(
        settings,
        os.environ if environment is None else environment,
    )
    factory = adapter_factory or get_provider_turn_adapter
    adapter = factory()
    run_budget = budget or LiveEvaluationRunBudget()
    corpus = get_mbb_evaluation_corpus()
    controller = LiveJourneyController(
        adapter,
        source_factory=lambda provider, profile: ProductionJourneyObservationSource(
            provider,
            case_service_factory=lambda bounded, case: _case_service(
                bounded,
                case,
                run_budget,
            ),
            reasoning_profile=profile,
        ),
        policy_version=AI_SYSTEM_POLICY_VERSION,
        budget=run_budget,
        explicitly_authorized=True,
    )
    matrix = await controller.run(
        corpus,
        case_ids=LIVE_JOURNEY_CASE_IDS,
        reasoning_profiles=profiles,
    )
    return _serialize_report(matrix, pretty=args.pretty)


def _case_service(
    adapter: ProviderTurnAdapter,
    case: EvaluationCase,
    budget: LiveEvaluationRunBudget,
) -> AITurnService:
    fixtures = {item.capability_name: item for item in case.capability_fixtures}
    values: dict[str, object | Exception] = {}
    for name in case.exposed_capabilities:
        fixture = fixtures.get(name)
        if fixture is None:
            values[name] = SafeCapabilityError("evaluation_fixture_unavailable")
        elif fixture.status == "success":
            values[name] = fixture.output
        else:
            values[name] = SafeCapabilityError(
                fixture.safe_code or fixture.error_category or "fixture_failed"
            )
    return AITurnService(
        adapter,
        capability_registry=fixture_registry(values),
        authority_checker=_evaluation_authority,
        limits=AITurnLimits(
            provider_calls=budget.max_provider_calls_per_case,
            tool_rounds=budget.max_tool_rounds_per_case,
            capability_executions=budget.max_capability_executions_per_case,
        ),
    )


async def _evaluation_authority(_context: object) -> bool:
    return True


def _serialize_report(report: BaseModel, *, pretty: bool) -> str:
    return json.dumps(
        report.model_dump(mode="json"),
        ensure_ascii=True,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
    )


def _write_output(path: Path, output: str) -> None:
    if path.exists():
        raise LiveEvaluationConfigurationError("output_already_exists")
    path.write_text(f"{output}\n", encoding="utf-8")


def _profiles(values: Sequence[str] | None) -> tuple[ProviderReasoningProfile, ...]:
    if not values:
        raise LiveEvaluationConfigurationError("reasoning_profile_required")
    profiles = tuple(ProviderReasoningProfile(value) for value in values)
    if len(profiles) != len(set(profiles)):
        raise LiveEvaluationConfigurationError("reasoning_profiles_must_be_unique")
    return profiles


def _validate_live_settings(
    settings: Settings,
    environment: Mapping[str, str],
) -> None:
    if settings.ai_adapter != "disabled":
        raise LiveEvaluationConfigurationError("legacy_ai_must_be_disabled")
    if settings.ai_turn_provider != "deepseek":
        raise LiveEvaluationConfigurationError("deepseek_provider_not_selected")
    if any(
        (
            settings.whatsapp_send_enabled,
            settings.crm_send_enabled,
            settings.payment_send_enabled,
            settings.relance_enabled,
            settings.scheduled_tasks_enabled,
            settings.m1_maps_fanout_enabled,
        )
    ):
        raise LiveEvaluationConfigurationError("external_effect_gates_not_disabled")
    process_key = environment.get("DEEPSEEK_API_KEY", "").strip()
    if not process_key or process_key != settings.deepseek_api_key.strip():
        raise LiveEvaluationConfigurationError("provider_credentials_unavailable")


def main() -> int:
    args = build_parser().parse_args()
    try:
        output = asyncio.run(_run(args))
        if args.output is None:
            print(output)
        else:
            _write_output(args.output, output)
    except LiveEvaluationBudgetExceeded as exc:
        if exc.evidence is not None:
            failure_output = _serialize_report(
                LiveEvaluationFailureReport(failure=exc.evidence),
                pretty=args.pretty,
            )
            if args.output is None:
                print(failure_output)
            else:
                _write_output(args.output, failure_output)
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1
    except LiveEvaluationProviderFailure as exc:
        failure_output = _serialize_report(
            LiveEvaluationFailureReport(failure=exc.evidence),
            pretty=args.pretty,
        )
        if args.output is None:
            print(failure_output)
        else:
            _write_output(args.output, failure_output)
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1
    except LiveEvaluationConfigurationError as exc:
        print(f"Evaluation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # Never include replay, provider, or credential data.
        print(f"Evaluation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
