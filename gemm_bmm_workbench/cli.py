from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

from .canonical import atomic_write_json, read_jsonl
from .errors import WorkbenchError
from .probes import run_diagnostic_probes
from .resources import Budget, CommitLock
from .sampling import generate_sample_tree
from .selfcheck import run_selfcheck
from .runtime import Workbench


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def budget_from_args(args: argparse.Namespace) -> Budget:
    values: dict[str, Any] = {}
    for field in ("max_jobs_per_stage", "max_parallel_jobs", "resource_policy"):
        value = getattr(args, field, None)
        if value is not None:
            values[field] = value
    buffer_mb = getattr(args, "max_buffer_mb", None)
    if buffer_mb is not None:
        values["max_accum_buffer_bytes"] = int(buffer_mb * 1024 * 1024)
    return Budget(**values).validated()


def add_budget_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-jobs-per-stage", type=int, default=None)
    parser.add_argument("--max-parallel-jobs", type=int, default=None)
    parser.add_argument("--max-buffer-mb", type=int, default=None)
    parser.add_argument("--resource-policy", choices=["degrade", "error"], default=None)


def cmd_generate_samples(args: argparse.Namespace) -> dict[str, Any]:
    return generate_sample_tree(args.out)


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    records = read_jsonl(args.jobs)
    bench = Workbench(args.state_dir, budget=budget_from_args(args))
    return bench.submit_records(
        records,
        parallel_jobs=args.parallel_jobs,
        npz_path=args.matrices,
        kill_after_chunks=args.kill_after_chunks,
    )


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    bench = Workbench(args.state_dir, budget=budget_from_args(args))
    return bench.run_pending()


def cmd_query_cache(args: argparse.Namespace) -> dict[str, Any]:
    bench = Workbench(args.state_dir)
    return bench.query_cache(args.job_id)


def cmd_selfcheck(args: argparse.Namespace) -> dict[str, Any]:
    report = run_selfcheck(args.state_dir)
    atomic_write_json(Path(args.state_dir) / "selfcheck.json", report)
    report["cli_exit_code"] = 0 if report["ok"] else 3
    return report


def cmd_migrate_ledger(args: argparse.Namespace) -> dict[str, Any]:
    bench = Workbench(args.state_dir)
    state = bench.load_committed_state()
    enrichment = {
        job_id: {"input_hash": job.get("input_hash", ""), "result_hash": job.get("result_hash", "")}
        for job_id, job in state.get("jobs", {}).items()
    }
    if args.direction == "v1-to-v2":
        return bench.ledger.migrate_v1_to_v2(enrichment)
    return bench.ledger.migrate_v2_to_v1(allow_version_regression=not args.deny_downgrade)


def cmd_probes(args: argparse.Namespace) -> dict[str, Any]:
    bench = Workbench(args.state_dir, budget=budget_from_args(args))
    return run_diagnostic_probes(bench)


def cmd_hold_lock(args: argparse.Namespace) -> dict[str, Any]:
    lock = CommitLock(Path(args.state_dir) / "locks" / "commit.lock").acquire()
    try:
        print_json({"locked": True, "seconds": args.seconds})
        time.sleep(args.seconds)
    finally:
        lock.release()
    return {"locked": False}


def cmd_lock_probe(args: argparse.Namespace) -> dict[str, Any]:
    with CommitLock(Path(args.state_dir) / "locks" / "commit.lock"):
        return {"acquired": True}


def cmd_demo(args: argparse.Namespace) -> dict[str, Any]:
    from .demo import build_demo

    return build_demo(base_dir=args.workspace, state_dir=args.state_dir)


def cmd_recovery_demo(args: argparse.Namespace) -> dict[str, Any]:
    from .demo import recovery_demo

    return recovery_demo(base_dir=args.workspace, state_dir=args.state_dir)


def cmd_serve(args: argparse.Namespace) -> dict[str, Any]:
    from .web import serve

    return serve(args.state_dir, host=args.host, port=args.port)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wb", description="GEMM/BMM incremental operator diagnostics workbench")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-samples", help="generate deterministic JSONL and NPZ samples")
    generate.add_argument("--out", default="samples")
    generate.set_defaults(func=cmd_generate_samples)

    run = sub.add_parser("run", help="validate and execute a JSONL manifest as one submission")
    run.add_argument("--jobs", type=Path, required=True)
    run.add_argument("--state-dir", default="runs/demo")
    run.add_argument("--matrices", type=Path, default=None)
    run.add_argument("--parallel-jobs", type=int, default=1)
    run.add_argument("--kill-after-chunks", type=int, default=None)
    add_budget_arguments(run)
    run.set_defaults(func=cmd_run)

    resume = sub.add_parser("resume", help="resume pending run from latest committed snapshot")
    resume.add_argument("--state-dir", default="runs/demo")
    add_budget_arguments(resume)
    resume.set_defaults(func=cmd_resume)

    query = sub.add_parser("query-cache", help="query cache hits and key composition for one job")
    query.add_argument("--state-dir", default="runs/demo")
    query.add_argument("--job-id", required=True)
    query.set_defaults(func=cmd_query_cache)

    check = sub.add_parser("selfcheck", help="run cross-module consistency self-check")
    check.add_argument("--state-dir", default="runs/demo")
    check.set_defaults(func=cmd_selfcheck)

    migrate = sub.add_parser("migrate-ledger", help="migrate ledger schema v1<->v2")
    migrate.add_argument("--state-dir", default="runs/demo")
    migrate.add_argument("--direction", choices=["v1-to-v2", "v2-to-v1"], required=True)
    migrate.add_argument("--deny-downgrade", action="store_true")
    migrate.set_defaults(func=cmd_migrate_ledger)

    probes = sub.add_parser("probes", help="run expected business-error and tolerance probes")
    probes.add_argument("--state-dir", default="runs/demo")
    add_budget_arguments(probes)
    probes.set_defaults(func=cmd_probes)

    hold = sub.add_parser("hold-lock", help="hold commit lock for concurrency test")
    hold.add_argument("--state-dir", default="runs/demo")
    hold.add_argument("--seconds", type=float, default=5.0)
    hold.set_defaults(func=cmd_hold_lock)

    lock_probe = sub.add_parser("lock-probe", help="attempt non-blocking commit lock")
    lock_probe.add_argument("--state-dir", default="runs/demo")
    lock_probe.set_defaults(func=cmd_lock_probe)

    demo = sub.add_parser("demo", help="build full deterministic incremental demo and diagnostics")
    demo.add_argument("--workspace", default=".")
    demo.add_argument("--state-dir", default="runs/demo")
    demo.set_defaults(func=cmd_demo)

    recovery = sub.add_parser("recovery-demo", help="subprocess SIGKILL and restart recovery demo")
    recovery.add_argument("--workspace", default=".")
    recovery.add_argument("--state-dir", default="runs/recovery-demo")
    recovery.set_defaults(func=cmd_recovery_demo)

    serve = sub.add_parser("serve", help="start local diagnostics web workbench")
    serve.add_argument("--state-dir", default="runs/demo")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.set_defaults(func=cmd_serve)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = args.func(args)
        if args.command == "serve":
            return 0
        print_json(result)
        if args.command == "selfcheck" and not result.get("ok", False):
            return 3
        return 0
    except WorkbenchError as exc:
        state_dir = getattr(args, "state_dir", "runs/demo")
        try:
            bench = Workbench(state_dir)
            record = bench.record_error(exc, source=f"cli:{args.command}")
        except Exception:
            record = exc.to_dict()
        print_json(record)
        return 2
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001
        print_json({"ok": False, "code": "E_INTERNAL", "message": str(exc), "type": exc.__class__.__name__})
        return 1


if __name__ == "__main__":
    sys.exit(main())
