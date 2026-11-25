"""Command-line entry point for the diagnostic workbench."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .cache import ContentAddressedCache
from .errors import WorkbenchError
from .report import validate_report
from .runtime import run_workbench
from .sample_generator import generate


def _emit(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _run(args: argparse.Namespace) -> int:
    report = run_workbench(
        repository_path=args.repository,
        requests_path=args.requests,
        schedule_path=args.schedule,
        corpus_path=args.corpus,
        cache_dir=args.cache,
        output_json=args.out_json,
        output_html=args.out_html,
        baseline_repository_path=args.baseline_repository,
        baseline_schedule_path=args.baseline_schedule,
    )
    _emit(
        {
            "business_code": report["business_code"],
            "business_codes": report["summary"]["business_codes"],
            "requests_total": report["summary"]["requests_total"],
            "requests_ok": report["summary"]["requests_ok"],
            "cache_hit_requests": report["summary"]["cache_hit_requests"],
            "block_equivalence": report["block_equivalence"]["status"],
            "selected_profile": report["summary"]["selected_profile"],
            "repository_snapshot_version": report["summary"][
                "repository_snapshot_version"
            ],
            "snapshot_id": report["snapshot"]["snapshot_id"],
            "invalidation_counts": (
                report["invalidation"]["counts"]
                if report["invalidation"] is not None
                else None
            ),
            "report_json": str(args.out_json) if args.out_json else None,
            "report_html": str(args.out_html) if args.out_html else None,
        }
    )
    return 0


def _generate(args: argparse.Namespace) -> int:
    _emit(generate(args.out))
    return 0


def _snapshots(args: argparse.Namespace) -> int:
    cache = ContentAddressedCache(args.cache)
    active = cache.active_snapshot()
    _emit(
        {
            "active_snapshot_id": (
                active["snapshot_id"] if active is not None else None
            ),
            "snapshots": cache.list_snapshots(),
        }
    )
    return 0


def _activate(args: argparse.Namespace) -> int:
    cache = ContentAddressedCache(args.cache)
    record = cache.activate_snapshot(args.snapshot_id)
    _emit(
        {
            "activated": record["snapshot_id"],
            "manifest_hash": record["manifest_hash"],
        }
    )
    return 0


def _verify(args: argparse.Namespace) -> int:
    try:
        report = json.loads(
            Path(args.report).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkbenchError(
            "E_INCONSISTENT",
            "diagnostic report cannot be loaded",
            error=str(exc),
        ) from exc
    cache = ContentAddressedCache(args.cache) if args.cache else None
    result = validate_report(report, cache=cache)
    _emit({"report": str(args.report), **result})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ragdiag",
        description="Deterministic RAG/tool-call diagnostic workbench",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser(
        "generate", help="generate deterministic sample inputs"
    )
    generate_parser.add_argument("--out", default="data")
    generate_parser.set_defaults(handler=_generate)

    run_parser = subparsers.add_parser(
        "run", help="execute a complete diagnostic run"
    )
    run_parser.add_argument("--repository", default="data/tool_results.json")
    run_parser.add_argument("--requests", default="data/requests.jsonl")
    run_parser.add_argument("--schedule", default="data/schedule.json")
    run_parser.add_argument(
        "--corpus", default="data/evidence_corpus.jsonl"
    )
    run_parser.add_argument("--cache", default=".workbench-cache")
    run_parser.add_argument(
        "--out-json", default="reports/diagnostic.json"
    )
    run_parser.add_argument(
        "--out-html", default="reports/diagnostic.html"
    )
    run_parser.add_argument("--baseline-repository")
    run_parser.add_argument("--baseline-schedule")
    run_parser.set_defaults(handler=_run)

    invalidate_parser = subparsers.add_parser(
        "invalidate",
        help="run current inputs with invalidation against a baseline repository",
    )
    invalidate_parser.add_argument(
        "--repository", default="data/tool_results.next.json"
    )
    invalidate_parser.add_argument("--requests", default="data/requests.jsonl")
    invalidate_parser.add_argument("--schedule", default="data/schedule.json")
    invalidate_parser.add_argument(
        "--corpus", default="data/evidence_corpus.jsonl"
    )
    invalidate_parser.add_argument("--cache", default=".workbench-cache")
    invalidate_parser.add_argument(
        "--out-json", default="reports/invalidation.json"
    )
    invalidate_parser.add_argument(
        "--out-html", default="reports/invalidation.html"
    )
    invalidate_parser.add_argument(
        "--baseline-repository", default="data/tool_results.json"
    )
    invalidate_parser.add_argument("--baseline-schedule")
    invalidate_parser.set_defaults(handler=_run)

    snapshots_parser = subparsers.add_parser(
        "snapshots", help="list snapshots and the active pointer"
    )
    snapshots_parser.add_argument("--cache", default=".workbench-cache")
    snapshots_parser.set_defaults(handler=_snapshots)

    activate_parser = subparsers.add_parser(
        "activate", help="atomically activate a versioned snapshot"
    )
    activate_parser.add_argument("snapshot_id")
    activate_parser.add_argument("--cache", default=".workbench-cache")
    activate_parser.set_defaults(handler=_activate)

    verify_parser = subparsers.add_parser(
        "verify", help="independently validate an existing JSON report"
    )
    verify_parser.add_argument(
        "--report", default="reports/diagnostic.json"
    )
    verify_parser.add_argument("--cache")
    verify_parser.set_defaults(handler=_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except WorkbenchError as exc:
        print(
            json.dumps(
                exc.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "code": "E_INCONSISTENT",
                    "message": "unexpected command failure",
                    "details": {"error": repr(exc)},
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

