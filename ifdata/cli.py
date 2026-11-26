"""Command-line and interactive entry points for the construction editor."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from .errors import BusinessError, ForcedTermination
from .generator import generate_fixture
from .repl import run_repl
from .scheduler import plan_admission
from .strategy import (
    V1StrategyStore,
    V2StrategyStore,
    locate_strategy,
    migrate_v1_to_v2,
    migrate_v2_to_v1,
)
from .types import BatchEstimate, BudgetLimits
from .utils import json_pretty


def _print(value: Any) -> None:
    sys.stdout.write(json_pretty(value))


def _add_engine_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--data", default="data", help="input data directory")
    parser.add_argument("--state", default=".ifdata-state", help="state directory")
    parser.add_argument("--out", default="export", help="export directory")
    parser.add_argument(
        "--strategy-generation",
        type=int,
        choices=(1, 2),
        default=2,
    )
    parser.add_argument("--strategy-path")


def _build_engine(args: argparse.Namespace):
    from .engine import PipelineEngine

    return PipelineEngine(
        data_dir=args.data,
        state_dir=args.state,
        out_dir=args.out,
        strategy_generation=args.strategy_generation,
        strategy_path=args.strategy_path,
    )


def _parse_failpoints(values: list[str] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values or []:
        key, separator, message = value.partition("=")
        if not separator or ":" not in key:
            raise BusinessError(
                "E_INVALID_INPUT",
                "failpoint must be STAGE:BATCH=message",
                details={"value": value},
            )
        result[key] = message
    return result


def command_generate(args: argparse.Namespace) -> int:
    manifest = generate_fixture(
        args.out,
        seed=args.seed,
        count=args.count,
        force=args.force,
    )
    _print(
        {
            "status": "generated",
            "out": str(Path(args.out)),
            "seed": manifest["seed"],
            "rows": manifest["sample_rows"],
            "strategy": manifest["strategy"],
        }
    )
    return 0


def command_run(args: argparse.Namespace) -> int:
    engine = _build_engine(args)
    report = engine.run(
        resume=True,
        reset=args.fresh,
        requested_workers=args.workers,
        compare_budget=args.compare_budget,
        memory_limit_bytes=args.memory_limit,
        stop_after_stage=args.stop_after,
        fail_stage=args.fail_stage,
        failpoints=_parse_failpoints(args.failpoint),
    )
    _print(report)
    return 0


def command_resume(args: argparse.Namespace) -> int:
    args.fresh = False
    return command_run(args)


def command_status(args: argparse.Namespace) -> int:
    _print(_build_engine(args).status())
    return 0


def command_show_stage(args: argparse.Namespace) -> int:
    _print(_build_engine(args).show_stage(args.stage))
    return 0


def command_budget_check(args: argparse.Namespace) -> int:
    limits = BudgetLimits(
        max_workers=args.max_workers,
        compare_budget=args.compare_budget,
        memory_limit_bytes=args.memory_limit,
        error_priority=tuple(args.priority.split(",")),
    )
    batch = BatchEstimate(
        batch_id="candidate-batch",
        sample_count=args.sample_count,
        comparisons=args.comparisons,
        memory_bytes=args.memory,
        requested_workers=args.workers,
    )
    plan = plan_admission([batch], limits)
    output = plan.to_dict()
    output["primary_error_code"] = (
        plan.rejected[0]["code"] if plan.rejected else None
    )
    output["limits"] = {
        "max_workers": limits.max_workers,
        "compare_budget": limits.compare_budget,
        "memory_limit_bytes": limits.memory_limit_bytes,
        "error_priority": list(limits.error_priority),
    }
    _print(output)
    return 0 if plan.ok else 1


def command_strategy_info(args: argparse.Namespace) -> int:
    if args.path:
        path = Path(args.path)
    else:
        path = locate_strategy(args.data, args.generation)
    if args.generation == 1:
        snapshot = V1StrategyStore(path).load()
    else:
        snapshot = V2StrategyStore(path).load_active()
    _print(
        {
            "generation": args.generation,
            "path": str(path),
            "snapshot_version": snapshot["snapshot_version"],
            "strategy_id": snapshot["strategy_id"],
            "checksum": snapshot["checksum"],
            "priorities": snapshot.get("priorities"),
            "tombstones": snapshot.get("tombstones", []),
            "source": snapshot.get("source"),
            "config_keys": sorted(snapshot["config"]),
        }
    )
    return 0


def command_migrate_up(args: argparse.Namespace) -> int:
    source = Path(args.input)
    v1 = V1StrategyStore(source).load()
    upgraded = migrate_v1_to_v2(
        v1,
        source_path=str(source),
        fallback_ms=args.fallback_ms,
        snapshot_version=args.snapshot_version,
    )
    target_root = Path(args.output_store) if args.output_store else source.parent / "v2"
    target = V2StrategyStore(target_root)
    active = target.commit(upgraded, owner="cli-migrate-v1-v2")
    _print(
        {
            "status": "migrated",
            "from": {"generation": 1, "checksum": v1["checksum"]},
            "to": {
                "generation": 2,
                "path": str(target_root),
                "snapshot_version": active["snapshot_version"],
                "checksum": active["checksum"],
            },
            "completion_source": active.get("source"),
        }
    )
    return 0


def command_migrate_down(args: argparse.Namespace) -> int:
    source = Path(args.input_store)
    v2 = V2StrategyStore(source).load_active()
    target_path = Path(args.output)
    audit_path = (
        Path(args.audit)
        if args.audit
        else target_path.with_name("v1-downgrade-audit.json")
    )
    downgraded = migrate_v2_to_v1(
        v2,
        source_path=str(source),
        fallback_ms=args.fallback_ms,
        audit_path=audit_path,
    )
    store = V1StrategyStore(target_path)
    active = store.commit(downgraded, owner="cli-migrate-v2-v1")
    _print(
        {
            "status": "downgraded",
            "from": {
                "generation": 2,
                "checksum": v2["checksum"],
                "tombstones": len(v2.get("tombstones", [])),
            },
            "to": {
                "generation": 1,
                "path": str(target_path),
                "checksum": active["checksum"],
            },
            "discard_rule": downgraded["migration_note"]["discard_rule"],
            "audit_path": str(audit_path),
        }
    )
    return 0


def command_repl(args: argparse.Namespace) -> int:
    return run_repl(_build_engine(args))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ifdata",
        description=(
            "Deterministic six-stage LLM instruction-tuning data editor"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser(
        "generate", help="generate fixed-seed fixtures and strategies"
    )
    generate.add_argument("--out", default="data")
    generate.add_argument("--seed", type=int, default=20261127)
    generate.add_argument("--count", type=int, default=48)
    generate.add_argument("--force", action="store_true")
    generate.set_defaults(func=command_generate)

    run = subparsers.add_parser("run", help="run or continue the six stages")
    _add_engine_args(run)
    run.add_argument("--workers", type=int)
    run.add_argument("--compare-budget", type=int)
    run.add_argument("--memory-limit", type=int)
    run.add_argument("--fresh", action="store_true")
    run.add_argument("--stop-after", choices=("ingest", "template", "quality", "dedup", "sampling", "export"))
    run.add_argument("--fail-stage", choices=("ingest", "template", "quality", "dedup", "sampling", "export"))
    run.add_argument(
        "--failpoint",
        action="append",
        help="inject a worker failure, for example dedup:1=boom",
    )
    run.set_defaults(func=command_run)

    resume = subparsers.add_parser(
        "resume", help="resume from the last committed stage"
    )
    _add_engine_args(resume)
    resume.add_argument("--workers", type=int)
    resume.add_argument("--compare-budget", type=int)
    resume.add_argument("--memory-limit", type=int)
    resume.add_argument("--stop-after", choices=("ingest", "template", "quality", "dedup", "sampling", "export"))
    resume.add_argument("--fail-stage", choices=("ingest", "template", "quality", "dedup", "sampling", "export"))
    resume.add_argument("--failpoint", action="append")
    resume.add_argument("--fresh", action="store_true", help=argparse.SUPPRESS)
    resume.set_defaults(func=command_resume)

    status = subparsers.add_parser("status", help="show committed stages")
    _add_engine_args(status)
    status.set_defaults(func=command_status)

    show_stage = subparsers.add_parser(
        "show-stage", help="print a committed stage snapshot"
    )
    _add_engine_args(show_stage)
    show_stage.add_argument(
        "stage",
        choices=("ingest", "template", "quality", "dedup", "sampling", "export"),
    )
    show_stage.set_defaults(func=command_show_stage)

    budget = subparsers.add_parser(
        "budget-check", help="check one batch against all three budgets"
    )
    budget.add_argument("--workers", type=int, default=1)
    budget.add_argument("--comparisons", type=int, default=0)
    budget.add_argument("--memory", type=int, default=0)
    budget.add_argument("--sample-count", type=int, default=1)
    budget.add_argument("--max-workers", type=int, default=4)
    budget.add_argument("--compare-budget", type=int, default=1000)
    budget.add_argument("--memory-limit", type=int, default=4 * 1024 * 1024)
    budget.add_argument(
        "--priority",
        default=(
            "E_CONCURRENCY_LIMIT,E_COMPARE_BUDGET,E_MEMORY_LIMIT"
        ),
    )
    budget.set_defaults(func=command_budget_check)

    info = subparsers.add_parser(
        "strategy-info", help="inspect an active strategy snapshot"
    )
    info.add_argument("--data", default="data")
    info.add_argument("--path")
    info.add_argument("--generation", type=int, choices=(1, 2), default=2)
    info.set_defaults(func=command_strategy_info)

    migrate_up = subparsers.add_parser(
        "migrate-v1-to-v2", help="upgrade a v1 strategy snapshot"
    )
    migrate_up.add_argument("--input", required=True)
    migrate_up.add_argument("--output-store")
    migrate_up.add_argument("--snapshot-version", type=int)
    migrate_up.add_argument("--fallback-ms", type=int)
    migrate_up.set_defaults(func=command_migrate_up)

    migrate_down = subparsers.add_parser(
        "migrate-v2-to-v1", help="downgrade a v2 strategy snapshot"
    )
    migrate_down.add_argument("--input-store", required=True)
    migrate_down.add_argument("--output", required=True)
    migrate_down.add_argument("--audit")
    migrate_down.add_argument("--fallback-ms", type=int)
    migrate_down.set_defaults(func=command_migrate_down)

    repl = subparsers.add_parser(
        "repl", help="start the interactive construction editor"
    )
    _add_engine_args(repl)
    repl.set_defaults(func=command_repl)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except ForcedTermination as exc:
        sys.stderr.write(
            json_pretty(
                {
                    "status": "terminated",
                    "code": "E_FORCED_TERMINATION",
                    "stage": exc.stage,
                    "message": exc.reason,
                    "committed_state_preserved": True,
                }
            )
        )
        return 130
    except BusinessError as exc:
        sys.stderr.write(json_pretty({"status": "error", **exc.to_dict()}))
        return 2
    except FileNotFoundError as exc:
        sys.stderr.write(
            json_pretty(
                {
                    "status": "error",
                    "code": "E_INVALID_INPUT",
                    "message": str(exc),
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

