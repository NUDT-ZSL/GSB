from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from . import __version__
from .errors import LayerForgeError
from .runtime import (
    make_paths,
    parse_set_args,
    run_build,
    run_judge,
    run_verify,
    run_worker,
)
from .samplegen import generate_samples
from .store import Store


def _print_json(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False))


def _add_runtime_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--sample-dir", default="samples", help="directory containing v1/v2 samples")
    parser.add_argument("--workdir", default=".layerforge", help="state/object/root/lock directory")
    parser.add_argument("--report-dir", default=None, help="JSON report directory (default: <workdir>/reports)")
    parser.add_argument("--clock-ms", type=int, default=None, help="injected logical clock observation in milliseconds")


def _paths(args: argparse.Namespace):
    return make_paths(
        sample_dir=args.sample_dir,
        workdir=args.workdir,
        report_dir=args.report_dir,
    )


def _crash(args: argparse.Namespace) -> Optional[Dict[str, str]]:
    at = getattr(args, "crash_at", None)
    step_id = getattr(args, "crash_step", None)
    if not at:
        return None
    if not step_id:
        raise LayerForgeError("--crash-at requires --crash-step")
    return {"at": at, "step_id": step_id}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="layerforge",
        description="Deterministic layered image build/cache adjudication workbench",
    )
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate", help="generate deterministic seed samples")
    generate.add_argument("--output", default="samples", type=Path)
    generate.add_argument("--seed", default=20261126, type=int)

    build = sub.add_parser("build", help="merge manifests, materialize layers, adjudicate and commit")
    _add_runtime_options(build)
    build.add_argument("--concurrency", type=int, default=None)
    build.add_argument("--round-budget", type=int, default=None, dest="round_budget_ms")
    build.add_argument("--set-arg", action="append", default=[], help="override NAME=VALUE; repeatable")
    build.add_argument("--invalidate", action="append", default=[], help="explicitly invalidate STEP_ID; repeatable")
    build.add_argument("--crash-at", choices=["temp_object", "object_renamed", "materialization_committed", "cache_commit"])
    build.add_argument("--crash-step")
    build.add_argument("--owner")

    judge = sub.add_parser("judge", help="output reuse/invalidation/sampling decisions without building")
    _add_runtime_options(judge)
    judge.add_argument("--set-arg", action="append", default=[])
    judge.add_argument("--invalidate", action="append", default=[])

    resume = sub.add_parser("resume", help="recover interrupted objects/state and continue build")
    _add_runtime_options(resume)
    resume.add_argument("--concurrency", type=int, default=None)
    resume.add_argument("--round-budget", type=int, default=None, dest="round_budget_ms")
    resume.add_argument("--set-arg", action="append", default=[])
    resume.add_argument("--invalidate", action="append", default=[])
    resume.add_argument("--owner")

    status = sub.add_parser("status", help="show SQLite snapshot, phases, watermarks and schedules")
    _add_runtime_options(status)

    verify = sub.add_parser("verify", help="verify canonical objects, digests and optional reference run")
    _add_runtime_options(verify)
    verify.add_argument("--reference-dir", default=None, type=Path)

    worker = sub.add_parser("worker", help="internal single-step worker for multiprocess checks")
    _add_runtime_options(worker)
    worker.add_argument("--step", required=True)
    worker.add_argument("--set-arg", action="append", default=[])
    worker.add_argument("--crash-at", choices=["temp_object", "object_renamed", "materialization_committed", "cache_commit"])
    worker.add_argument("--owner")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "generate":
            result = generate_samples(args.output, seed=args.seed)
            _print_json({"ok": True, "command": "generate", **result})
            return 0

        paths = _paths(args)

        if args.command == "judge":
            report = run_judge(
                paths,
                overrides=parse_set_args(args.set_arg),
                explicit_roots=args.invalidate,
            )
            _print_json({"command": "judge", **report})
            return 0

        if args.command in {"build", "resume"}:
            report = run_build(
                paths,
                overrides=parse_set_args(args.set_arg),
                explicit_roots=args.invalidate,
                concurrency=args.concurrency,
                round_budget_ms=args.round_budget_ms,
                crash=_crash(args) if args.command == "build" else None,
                owner=getattr(args, "owner", None),
                injected_clock_ms=getattr(args, "clock_ms", None),
            )
            output = {
                "command": args.command,
                "ok": report["ok"],
                "status": report["status"],
                "exit_code": report["exit_code"],
                "rounds": report.get("rounds"),
                "object_count": report.get("object_count"),
                "snapshot_version": report.get("snapshot_version"),
                "judge_summary": report.get("judge_summary"),
                "recovered": report.get("recovered"),
                "report_path": str(paths.report_dir / f"{args.command}.json" if args.command == "build" else paths.report_dir / "build.json"),
                "digests_path": str(paths.report_dir / "digests.json"),
            }
            if report["status"] == "budget_abandoned":
                output["abandoned"] = report["schedule"]["abandoned"]
            if report["status"] == "failed":
                output["failed"] = report["schedule"]["failed"]
            _print_json(output)
            return int(report["exit_code"])

        if args.command == "status":
            store = Store(paths.state_db)
            result = store.status()
            _print_json({"command": "status", "ok": True, **result})
            return 0

        if args.command == "verify":
            report = run_verify(paths, reference_dir=args.reference_dir)
            _print_json({"command": "verify", **report})
            return 0 if report["ok"] else 1

        if args.command == "worker":
            result = run_worker(
                paths,
                args.step,
                overrides=parse_set_args(args.set_arg),
                crash=_crash(args),
                owner=args.owner,
                injected_clock_ms=args.clock_ms,
            )
            _print_json({"command": "worker", **result})
            return 0

        parser.error(f"unknown command: {args.command}")
        return 2
    except LayerForgeError as exc:
        payload = {"command": getattr(args, "command", None), **exc.to_dict()}
        _print_json(payload)
        return exc.exit_code()
    except Exception as exc:
        _print_json(
            {
                "command": getattr(args, "command", None),
                "ok": False,
                "error_code": "E_INTERNAL",
                "message": str(exc),
                "type": type(exc).__name__,
            }
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
