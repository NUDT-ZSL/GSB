"""Command-line entry points for sample generation, routing, and demo runs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .cache import ContentAddressedCache
from .demo import run_demo, run_generate
from .errors import MoeError
from .models import ResourceLimits
from .routing import RoutingSession
from .validation import load_cases
from .weights import build_weight_bundle


def _write_json(result: dict, path: str | None) -> None:
    text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
    if path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(text, encoding="utf-8")
    print(text, end="")


def _limits(args: argparse.Namespace) -> ResourceLimits:
    return ResourceLimits(
        max_case_resident_bytes=args.max_case_bytes,
        max_parallel_recompute_blocks=args.max_parallel_recompute_blocks,
    )


def _command_generate(args: argparse.Namespace) -> int:
    result = run_generate(
        cases_path=args.out,
        manifest_path=args.manifest,
        seed=args.seed,
        count=args.count,
    )
    _write_json({"ok": True, **result}, args.report)
    return 0


def _command_route(args: argparse.Namespace) -> int:
    cases = load_cases(args.cases)
    cache = ContentAddressedCache(args.cache)
    limits = _limits(args)
    reports = []
    for case in cases:
        bundle = build_weight_bundle(case)
        session = RoutingSession(bundle, cache, limits=limits)
        session.compute_next_block()
        query_zero = session.query_block(0)
        session.route_all()
        for index in range(session.block_count):
            session.query_block(index)
        report = session.consistency_report(
            tolerance=args.tolerance, raise_on_mismatch=True
        )
        reports.append(
            {
                "case_id": case.case_id,
                "first_block_query_ms": query_zero["first_block_query_ms"],
                "first_block_expert_revisions": query_zero[
                    "expert_revisions_used"
                ],
                "first_block_capacity_summary": query_zero["capacity_summary"],
                "block_count": session.block_count,
                "consistency": report,
            }
        )
    _write_json(
        {
            "ok": True,
            "case_count": len(reports),
            "all_consistent": all(item["consistency"]["consistent"] for item in reports),
            "cases": reports,
        },
        args.out,
    )
    return 0


def _command_demo(args: argparse.Namespace) -> int:
    report = run_demo(
        cases_path=args.cases,
        workdir=args.workdir,
        reset=args.reset,
        limits=_limits(args),
        tolerance=args.tolerance,
    )
    if args.out:
        destination = Path(args.out)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    summary = {
        "ok": True,
        "all_consistent": report["all_consistent"],
        "case_count": report["case_count"],
        "invalidation_plan": (
            None if report["invalidation"] is None else report["invalidation"]["plan"]
        ),
        "cache_object_integrity": report["cache_object_integrity"],
        "report": args.out or str(Path(args.workdir) / "validation_report.json"),
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def _add_common_limits(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--max-case-bytes",
        type=int,
        default=64 * 1024 * 1024,
        help="per-case resident block output/audit memory limit",
    )
    parser.add_argument(
        "--max-parallel-recompute-blocks",
        type=int,
        default=2,
        help="maximum worker count used for parallel block recomputation",
    )
    parser.add_argument("--tolerance", type=float, default=1e-12)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="moe-router")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic JSONL samples")
    generate.add_argument("--out", default="samples/cases.jsonl")
    generate.add_argument("--manifest", default="samples/weight_manifest.json")
    generate.add_argument("--report", default=None)
    generate.add_argument("--seed", type=int, default=20261126)
    generate.add_argument("--count", type=int, default=6)
    generate.set_defaults(handler=_command_generate)

    route = subparsers.add_parser("route", help="route a case manifest and audit consistency")
    route.add_argument("--cases", default="samples/cases.jsonl")
    route.add_argument("--cache", default="artifacts/cache")
    route.add_argument("--out", default=None)
    _add_common_limits(route)
    route.set_defaults(handler=_command_route)

    demo = subparsers.add_parser("demo", help="run the full routing/invalidation/snapshot demo")
    demo.add_argument("--cases", default="samples/cases.jsonl")
    demo.add_argument("--workdir", default="artifacts/demo")
    demo.add_argument("--out", default="artifacts/validation_report.json")
    demo.add_argument("--reset", action="store_true")
    _add_common_limits(demo)
    demo.set_defaults(handler=_command_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except MoeError as exc:
        print(json.dumps(exc.to_dict(), indent=2, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
