from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any

from .errors import BusinessError, ErrorCode
from .scheduler import run_schedule, status_report
from .storage import StateStore
from .util import pretty_json
from .verification import verify_run
from .workload import load_workload


def _print(value: Any) -> None:
    print(pretty_json(value))


def _resolve_job(workload: Any, job_name: str | None) -> Any:
    jobs = workload.jobs
    if job_name is None:
        if len(jobs) != 1:
            raise BusinessError(
                ErrorCode.INVALID_WORKLOAD,
                "multiple jobs are loaded; pass --job",
                details={"jobs": sorted(job.name for job in jobs)},
            )
        return jobs[0]
    job = workload.job_map().get(job_name)
    if job is None:
        raise BusinessError(
            ErrorCode.UNKNOWN_JOB,
            f"unknown job {job_name!r}",
            details={"job": job_name, "available": sorted(job.name for job in jobs)},
        )
    return job


def _command_load(args: argparse.Namespace) -> int:
    workload = load_workload(args.input)
    store = StateStore(args.state_dir)
    result = store.load(workload, clock=args.logical_clock_ms)
    raw_digest = hashlib.sha256(Path(args.input).read_bytes()).hexdigest()
    result["input_sha256"] = raw_digest
    result["config"] = {
        "logical_time_budget_ms": workload.config.logical_time_budget_ms,
        "per_dataset_prefetch_limit": workload.config.per_dataset_prefetch_limit,
        "global_memory_budget_mib": workload.config.global_memory_budget_mib,
        "cache_capacity_entries": workload.config.cache_capacity_entries,
        "logical_clock_step_ms": workload.config.logical_clock_step_ms,
    }
    _print(result)
    return 0


def _command_schedule(args: argparse.Namespace) -> int:
    workload = load_workload(args.input) if args.input else None
    store = StateStore(args.state_dir)
    if workload is not None:
        store.load(workload, clock=args.logical_clock_ms)
    else:
        workload = store.get_workload(args.workload_digest)
    job = _resolve_job(workload, args.job)
    result = run_schedule(
        store=store,
        workload=workload,
        job=job,
        run_id=args.run_id,
    )
    _print(result)
    return 0


def _command_resume(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)
    run = store.get_run(args.run_id)
    workload = store.get_workload(run["workload_digest"])
    job = workload.job_map().get(run["job_name"])
    if job is None:
        raise BusinessError(
            ErrorCode.STATE_CORRUPT,
            "persisted run references an unknown job",
            details={"run_id": run["run_id"], "job": run["job_name"]},
        )
    result = run_schedule(
        store=store,
        workload=workload,
        job=job,
        resume_run_id=run["run_id"],
    )
    _print(result)
    return 0


def _command_status(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)
    _print(status_report(store, args.run_id))
    return 0


def _command_verify(args: argparse.Namespace) -> int:
    store = StateStore(args.state_dir)
    result = verify_run(
        store,
        run_id=args.run_id,
        allow_incomplete=args.allow_incomplete,
    )
    _print(result)
    return 0 if result["ok"] else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shardpipe",
        description=(
            "Deterministic shard-cache, prefetch scheduling, and recovery "
            "control plane"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    load = subparsers.add_parser("load", help="validate and load workload JSON")
    load.add_argument("--input", required=True, help="workload JSON path")
    load.add_argument("--state-dir", default="state")
    load.add_argument("--logical-clock-ms", type=int, default=0)
    load.set_defaults(handler=_command_load)

    schedule = subparsers.add_parser(
        "schedule", help="run or continue a deterministic schedule"
    )
    schedule.add_argument("--input", help="optional workload JSON to load first")
    schedule.add_argument("--state-dir", default="state")
    schedule.add_argument("--workload-digest")
    schedule.add_argument("--job")
    schedule.add_argument("--run-id")
    schedule.add_argument("--logical-clock-ms", type=int, default=0)
    schedule.set_defaults(handler=_command_schedule)

    resume = subparsers.add_parser(
        "resume", help="resume the latest committed snapshot of a run"
    )
    resume.add_argument("--state-dir", default="state")
    resume.add_argument("--run-id", required=True)
    resume.set_defaults(handler=_command_resume)

    status = subparsers.add_parser("status", help="emit stable run status JSON")
    status.add_argument("--state-dir", default="state")
    status.add_argument("--run-id")
    status.set_defaults(handler=_command_status)

    verify = subparsers.add_parser(
        "verify", help="compare persisted batches with serial truth"
    )
    verify.add_argument("--state-dir", default="state")
    verify.add_argument("--run-id")
    verify.add_argument(
        "--allow-incomplete",
        action="store_true",
        help="do not fail only because resource decisions left batches missing",
    )
    verify.set_defaults(handler=_command_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except BusinessError as exc:
        print(pretty_json(exc.as_dict()), file=sys.stderr)
        return 2
    except BrokenPipeError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
