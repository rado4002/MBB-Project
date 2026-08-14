"""Score a recorded provider-neutral replay against the MBB evaluation corpus."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ai.evaluation import (  # noqa: E402
    EvaluationReplay,
    EvaluationRunner,
    ScriptedEvaluationSource,
)
from app.ai.evaluation_corpus import get_mbb_evaluation_corpus  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--replay",
        required=True,
        type=Path,
        help="JSON replay containing run metadata and normalized observations.",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        dest="case_ids",
        help="Run only this corpus case; may be repeated.",
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
    return report.model_dump_json(indent=2 if args.pretty else None)


def main() -> int:
    args = build_parser().parse_args()
    try:
        output = asyncio.run(_run(args))
        if args.output is None:
            print(output)
        else:
            args.output.write_text(f"{output}\n", encoding="utf-8")
    except Exception as exc:  # The CLI reports no replay content or secrets.
        print(f"Evaluation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
