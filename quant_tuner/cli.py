from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config_io import migrate_config
from .engine import TuningEngine
from .errors import TuningError
from .estimator import load_platform_stats
from .planner import load_policy, load_tasks
from .propagation import (
    execute_quant_cache,
    plan_propagation,
    plan_v2_config_propagation,
    seed_quant_cache,
)
from .recovery import recover_workspace
from .sample_data import generate_all
from .self_check import self_check


def _print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _load_inputs(args: argparse.Namespace):
    tasks = load_tasks(args.tasks)
    platform = load_platform_stats(args.platform)
    policy = load_policy(args.config)
    return tasks, platform, policy


def cmd_generate_samples(args: argparse.Namespace) -> dict[str, Any]:
    paths = generate_all(args.output)
    _print_json({"ok": True, "paths": paths})
    return paths


def cmd_migrate_config(args: argparse.Namespace) -> dict[str, Any]:
    bundle = migrate_config(args.input, args.to, args.output, args.origin_ms)
    result = {
        "ok": True,
        "source_version": bundle.get("source_version"),
        "record_count": len(bundle["records"]),
        "target": args.to,
        "output": args.output,
    }
    _print_json(result)
    return result


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    tasks, platform, policy = _load_inputs(args)
    engine = TuningEngine(args.output, tasks, platform, policy)
    report = engine.run(resume=args.resume, stop_after_batches=args.stop_after_batches)
    compact = {
        "ok": True,
        "complete": report["complete"],
        "checkpoint_version": report["checkpoint_version"],
        "committed_point_count": report["committed_point_count"],
        "committed_batches": report["committed_batches"],
        "new_committed_batches": report["new_committed_batches"],
        "logical_clock_used_ms": report["logical_clock_used_ms"],
        "rejection_count": len(report["rejections"]),
        "best": report["best"],
        "report_path": report.get("report_path"),
        "recovery": report.get("recovery"),
    }
    _print_json(compact)
    return compact


def cmd_recover_info(args: argparse.Namespace) -> dict[str, Any]:
    info = recover_workspace(args.output)
    info.pop("snapshot", None)
    _print_json({"ok": True, **info})
    return info


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    tasks, platform, policy = _load_inputs(args)
    result = self_check(args.output, tasks, platform, policy, args.propagation)
    _print_json(result)
    return result


def cmd_propagate(args: argparse.Namespace) -> dict[str, Any]:
    old_tasks = load_tasks(args.old_tasks)
    new_tasks = load_tasks(args.new_tasks)
    if args.old_v2 and args.new_v2:
        document = plan_v2_config_propagation(
            args.old_v2, args.new_v2, old_tasks, new_tasks
        )
    else:
        document = plan_propagation(old_tasks, new_tasks)

    cache_result = None
    if args.cache:
        cache_result = execute_quant_cache(new_tasks, document, args.cache)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    result = {
        "ok": True,
        "output": str(output),
        "recompute_points": len(document["recompute_points"]),
        "reuse_points": len(document["reuse_points"]),
        "recompute_quant_summaries": document["recompute_quant_summaries"],
        "reuse_quant_summaries": document["reuse_quant_summaries"],
        "generation_bumps": document["generation_bumps"],
        "cache": cache_result,
    }
    _print_json(result)
    return result


def cmd_cache_summaries(args: argparse.Namespace) -> dict[str, Any]:
    tasks = load_tasks(args.tasks)
    result = seed_quant_cache(tasks, args.cache)
    output = {"ok": True, **result, "seeded_count": len(result["seeded"])}
    _print_json(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quant_tuner", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-samples")
    generate.add_argument("--output", default="samples")
    generate.set_defaults(func=cmd_generate_samples)

    migrate = sub.add_parser("migrate-config")
    migrate.add_argument("--input", required=True)
    migrate.add_argument("--to", required=True, choices=("unified", "v1", "v2"))
    migrate.add_argument("--output", required=True)
    migrate.add_argument("--origin-ms", type=int)
    migrate.set_defaults(func=cmd_migrate_config)

    run = sub.add_parser("run")
    run.add_argument("--tasks", required=True)
    run.add_argument("--platform", required=True)
    run.add_argument("--config", required=True)
    run.add_argument("--output", required=True)
    run.add_argument("--resume", action="store_true")
    run.add_argument("--stop-after-batches", type=int)
    run.set_defaults(func=cmd_run)

    recover_info = sub.add_parser("recover-info")
    recover_info.add_argument("--output", required=True)
    recover_info.set_defaults(func=cmd_recover_info)

    verify = sub.add_parser("verify")
    verify.add_argument("--tasks", required=True)
    verify.add_argument("--platform", required=True)
    verify.add_argument("--config", required=True)
    verify.add_argument("--output", required=True)
    verify.add_argument("--propagation")
    verify.set_defaults(func=cmd_verify)

    propagate = sub.add_parser("propagate")
    propagate.add_argument("--old-tasks", required=True)
    propagate.add_argument("--new-tasks", required=True)
    propagate.add_argument("--old-v2")
    propagate.add_argument("--new-v2")
    propagate.add_argument("--output", required=True)
    propagate.add_argument("--cache")
    propagate.set_defaults(func=cmd_propagate)

    cache = sub.add_parser("cache-summaries")
    cache.add_argument("--tasks", required=True)
    cache.add_argument("--cache", required=True)
    cache.set_defaults(func=cmd_cache_summaries)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        args.func(args)
        return 0
    except TuningError as exc:
        print(json.dumps(exc.to_dict(), ensure_ascii=False, indent=2, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
