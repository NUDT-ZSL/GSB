from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .canonical import canonical_json, read_json, read_jsonl
from .consistency import verify_run
from .demo import (
    run_budget_conflict_demo,
    run_fault_attribution_demo,
)
from .errors import DomainError, ErrorCode
from .executor import EXECUTION_STAGES
from .models import Budget, ExecutionConfig, Job, SchedulerConfig, ToleranceConfig
from .samplegen import (
    conflict_demo_jobs,
    default_sample_jobs,
    verify_samples,
    write_samples,
)
from .service import QConvService
from .web import Application, serve

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def default_config_path() -> Path:
    cwd_path = Path.cwd() / "config" / "default.json"
    if cwd_path.exists():
        return cwd_path
    return PROJECT_ROOT / "config" / "default.json"


def load_config(path: str | Path | None) -> dict[str, Any]:
    config_path = Path(path) if path else default_config_path()
    if not config_path.exists():
        return {}
    value = read_json(config_path)
    if not isinstance(value, dict):
        raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, "config must be a JSON object")
    return value


def _config_path(config: dict[str, Any], key: str, default: str) -> Path:
    value = Path(str(config.get(key, default)))
    return value if value.is_absolute() else Path.cwd() / value


def ensure_samples(config: dict[str, Any]) -> None:
    samples_dir = _config_path(config, "samples_dir", "samples")
    jobs_path = samples_dir / "jobs.jsonl"
    if jobs_path.exists():
        return
    seed = int(config.get("seed", 20261120))
    write_samples(
        samples_dir,
        default_sample_jobs(seed),
        seed=seed,
        extra_job_sets={"conflict_jobs": conflict_demo_jobs(seed)},
    )


def build_budget(config: dict[str, Any], args: argparse.Namespace) -> Budget:
    raw = {
        "mac_budget": 20_000_000,
        "memory_budget_bytes": 8_000_000,
        "worker_slots": 6,
        **dict(config.get("budget", {})),
    }
    for name in ("mac_budget", "memory_budget_bytes", "worker_slots"):
        value = getattr(args, name, None)
        if value is not None:
            raw[name] = value
    return Budget.from_dict(raw)


def build_scheduler(config: dict[str, Any], args: argparse.Namespace) -> SchedulerConfig:
    raw = dict(config.get("scheduler", {}))
    value = getattr(args, "max_workers_per_task", None)
    if value is not None:
        raw["max_workers_per_task"] = value
    if getattr(args, "no_overflow_protection", False):
        raw["overflow_protection"] = False
    return SchedulerConfig.from_dict(raw)


def build_tolerance(config: dict[str, Any], args: argparse.Namespace) -> ToleranceConfig:
    raw = dict(config.get("tolerance", {}))
    if getattr(args, "atol", None) is not None:
        raw["atol"] = args.atol
    if getattr(args, "rtol", None) is not None:
        raw["rtol"] = args.rtol
    return ToleranceConfig.from_dict(raw)


def parse_faults(values: list[str]) -> tuple[dict[str, Any], ...]:
    injections: list[dict[str, Any]] = []
    for value in values:
        parts = value.split(":")
        if len(parts) < 2 or len(parts) > 4:
            raise DomainError(
                ErrorCode.E_INVALID_JOB_FIELD,
                "fault format is job_id:chunk_id:stage[:code]",
            )
        job_id = parts[0]
        try:
            chunk_id = int(parts[1])
        except ValueError as exc:
            raise DomainError(
                ErrorCode.E_INVALID_JOB_FIELD, "fault chunk_id must be an integer"
            ) from exc
        stage = parts[2] if len(parts) >= 3 and parts[2] else "fixed_mac"
        if stage not in EXECUTION_STAGES:
            raise DomainError(
                ErrorCode.E_INVALID_JOB_FIELD,
                f"fault stage must be one of {list(EXECUTION_STAGES)}",
            )
        code = parts[3] if len(parts) == 4 and parts[3] else ErrorCode.E_INJECTED_FAULT
        injections.append(
            {
                "job_id": job_id,
                "chunk_id": chunk_id,
                "stage": stage,
                "code": code.value if isinstance(code, ErrorCode) else str(code),
                "message": f"CLI injected fault at {stage}",
            }
        )
    return tuple(injections)


def print_artifact(artifact: dict[str, Any], *, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(artifact, ensure_ascii=False, indent=2))
        return
    print(f"status={artifact['status']} code={artifact['code']}")
    if artifact.get("validation_issues"):
        for issue in artifact["validation_issues"]:
            print(f"  validation {issue['code']}: {issue['message']}")
        return
    plan = artifact.get("plan")
    if plan:
        print(
            "admission: "
            f"admitted={len(plan['admitted_job_ids'])} "
            f"rejected={len(plan['rejected_job_ids'])} "
            f"workers={plan['worker_slots_used']} chunks={plan['total_chunks']}"
        )
        for decision in plan["decisions"]:
            detail = ""
            if decision["status"] == "admitted":
                value = decision["plan"]
                detail = (
                    f" route={value['accumulator']['merge_mode']}"
                    f" workers={value['worker_count']}"
                    f" chunks={value['chunk_count']}"
                    f" mem={value['memory']['resident_bytes']}B"
                )
            print(f"  {decision['job_id']}: {decision['code']}{detail}")
    ledger = artifact.get("ledger")
    if ledger:
        print(
            "ledger: "
            f"mac={ledger['usage']['mac_committed']}/{ledger['limits']['mac_budget']} "
            f"memory={ledger['usage']['memory_committed_bytes']}/"
            f"{ledger['limits']['memory_budget_bytes']}B "
            f"workers={ledger['usage']['workers_committed']}/"
            f"{ledger['limits']['worker_slots']} "
            f"reconciled={ledger['reconciled']}"
        )
    execution = artifact.get("execution")
    if execution:
        print(
            "execution: "
            f"chunks_ok={execution['chunks_ok']} "
            f"chunks_failed={execution['chunks_failed']} "
            f"completed={execution['completed_job_ids']} "
            f"failed={execution['failed_job_ids']}"
        )
    for job in (artifact.get("error_spectrum") or {}).get("jobs", []):
        stages = ", ".join(
            f"{stage['stage']}={stage['max_abs_error']:.3e}"
            for stage in job["stages"]
        )
        print(
            f"  spectrum {job['job_id']}: dominant={job['dominant_stage']} "
            f"tolerance_ok={job['tolerance_ok']} violations={job['violation_count']}"
        )
        print(f"    {stages}")
    for issue in artifact.get("business_errors", []):
        location = ""
        if issue.get("chunk_id") is not None or issue.get("stage"):
            location = (
                f" chunk={issue.get('chunk_id')} stage={issue.get('stage')}"
            )
        print(f"  error {issue['code']}{location}: {issue['message']}")
    check = artifact.get("self_check")
    if check:
        print(f"self-check: {'PASS' if check['ok'] else 'FAIL'}")
        for issue in check.get("issues", []):
            print(f"  {issue['check']}: {issue['message']}")
    snapshot = artifact.get("snapshot")
    if snapshot:
        print(
            f"snapshot: generation={snapshot['generation']} "
            f"checksum={snapshot['snapshot_checksum'][:16]}… "
            f"path={snapshot['path']}"
        )
    if artifact.get("snapshot_error"):
        print(f"snapshot-error: {artifact['snapshot_error']}")


def cmd_generate_samples(args: argparse.Namespace, config: dict[str, Any]) -> int:
    seed = int(args.seed if args.seed is not None else config.get("seed", 20261120))
    root = Path(args.root) if args.root else _config_path(config, "samples_dir", "samples")
    manifest = write_samples(
        root,
        default_sample_jobs(seed),
        seed=seed,
        extra_job_sets={"conflict_jobs": conflict_demo_jobs(seed)},
    )
    print(
        canonical_json(
            {
                "ok": True,
                "samples": str(root),
                "jobs": sorted(manifest["jobs"]),
                "files": manifest["job_files"],
            }
        )
    )
    return 0


def cmd_verify_samples(args: argparse.Namespace, config: dict[str, Any]) -> int:
    root = Path(args.root) if args.root else _config_path(config, "samples_dir", "samples")
    seed = int(config.get("seed", 20261120))
    jobs = default_sample_jobs(seed) + conflict_demo_jobs(seed)
    problems = verify_samples(root, jobs)
    payload = {"ok": not problems, "problems": problems, "checked_jobs": len(jobs)}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not problems else 2


def cmd_run(args: argparse.Namespace, config: dict[str, Any]) -> int:
    ensure_samples(config)
    jobs_path = (
        Path(args.jobs)
        if args.jobs
        else _config_path(config, "samples_dir", "samples") / "jobs.jsonl"
    )
    try:
        records = read_jsonl(jobs_path)
    except ValueError as exc:
        raise DomainError(ErrorCode.E_INVALID_JOB_FIELD, str(exc)) from exc
    budget = build_budget(config, args)
    scheduler = build_scheduler(config, args)
    tolerance = build_tolerance(config, args)
    injections = parse_faults(args.fault)
    service = QConvService(
        _config_path(config, "snapshots_dir", "snapshots"),
        _config_path(config, "cache_dir", "cache"),
    )
    artifact = service.run_records(
        records,
        budget,
        scheduler,
        tolerance,
        ExecutionConfig(
            max_processes=budget.worker_slots, fault_injections=injections
        ),
        commit_snapshot=not args.no_snapshot,
    )
    print_artifact(artifact, as_json=args.json)
    return (
        0
        if artifact["status"]
        not in {"inconsistent", "validation_failed", "snapshot_error"}
        else 2
    )


def cmd_demo(args: argparse.Namespace, config: dict[str, Any]) -> int:
    service = QConvService(
        _config_path(config, "snapshots_dir", "snapshots"),
        _config_path(config, "cache_dir", "cache"),
    )
    artifacts: list[dict[str, Any]] = []
    if args.which in {"both", "budget"}:
        artifacts.append(run_budget_conflict_demo(service))
    if args.which in {"both", "fault"}:
        artifacts.append(run_fault_attribution_demo(service))
    if args.json:
        print(json.dumps(artifacts, ensure_ascii=False, indent=2))
    else:
        for index, artifact in enumerate(artifacts, start=1):
            print(f"=== demo {index}: {artifact['demo']['name']} ===")
            print_artifact(artifact)
            print()
    return (
        0
        if all(
            item["self_check"]
            and item["self_check"]["ok"]
            and not item.get("snapshot_error")
            for item in artifacts
        )
        else 2
    )


def cmd_serve(args: argparse.Namespace, config: dict[str, Any]) -> int:
    ensure_samples(config)
    jobs_path = (
        Path(args.jobs)
        if args.jobs
        else _config_path(config, "samples_dir", "samples") / "jobs.jsonl"
    )
    service = QConvService(
        _config_path(config, "snapshots_dir", "snapshots"),
        _config_path(config, "cache_dir", "cache"),
    )
    app = Application(
        service,
        jobs_path,
        build_budget(config, args),
        build_scheduler(config, args),
        build_tolerance(config, args),
    )
    serve(app, args.host, args.port)
    return 0


def cmd_self_check(args: argparse.Namespace, config: dict[str, Any]) -> int:
    service = QConvService(
        _config_path(config, "snapshots_dir", "snapshots"),
        _config_path(config, "cache_dir", "cache"),
    )
    snapshot = service.load_snapshot()
    if snapshot is None:
        raise DomainError(ErrorCode.E_NOT_FOUND, "no snapshot exists")
    components = snapshot["components"]
    plan = components["plan"]["payload"]
    ledger = components["budget_ledger"]["payload"]
    result = components["result_summary"]["payload"]
    jobs_by_id = {raw["job_id"]: Job.from_dict(raw) for raw in plan["jobs"]}
    check = verify_run(
        jobs_by_id,
        plan,
        ledger,
        result["execution"],
        result["error_spectrum"],
    )
    payload = {
        "ok": check["ok"],
        "generation": snapshot["generation"],
        "snapshot_checksum": snapshot["snapshot_checksum"],
        "checks": check["checks"],
        "issues": check["issues"],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if not check["ok"]:
        raise DomainError(
            ErrorCode.E_INCONSISTENT,
            "snapshot cross-module self-check failed",
            detail={"issues": check["issues"]},
        )
    return 0


def cmd_clean(args: argparse.Namespace, config: dict[str, Any]) -> int:
    service = QConvService(
        _config_path(config, "snapshots_dir", "snapshots"),
        _config_path(config, "cache_dir", "cache"),
    )
    print(json.dumps(service.clean(), ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qconv",
        description="Deterministic int8 quantized convolution budget laboratory",
    )
    parser.add_argument("--config", help="config JSON path")
    sub = parser.add_subparsers(dest="command", required=True)

    generate = sub.add_parser("generate-samples", help="write seeded sample JSONL/arrays")
    generate.add_argument("--root")
    generate.add_argument("--seed", type=int)
    generate.set_defaults(func=cmd_generate_samples)

    verify = sub.add_parser("verify-samples", help="verify saved arrays against seeds")
    verify.add_argument("--root")
    verify.set_defaults(func=cmd_verify_samples)

    run = sub.add_parser("run", help="execute a JSONL task file")
    run.add_argument("--jobs")
    run.add_argument("--mac-budget", type=int)
    run.add_argument("--memory-budget-bytes", type=int)
    run.add_argument("--worker-slots", type=int)
    run.add_argument("--max-workers-per-task", type=int)
    run.add_argument("--atol", type=float)
    run.add_argument("--rtol", type=float)
    run.add_argument("--no-overflow-protection", action="store_true")
    run.add_argument(
        "--fault",
        action="append",
        default=[],
        help="inject deterministic fault as job_id:chunk_id:stage[:code]",
    )
    run.add_argument("--no-snapshot", action="store_true")
    run.add_argument("--json", action="store_true")
    run.set_defaults(func=cmd_run)

    demo = sub.add_parser("demo", help="run built-in conflict and fault demos")
    demo.add_argument(
        "--which", choices=["both", "budget", "fault"], default="both"
    )
    demo.add_argument("--json", action="store_true")
    demo.set_defaults(func=cmd_demo)

    server = sub.add_parser("serve", help="start the local interactive web application")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8765)
    server.add_argument("--jobs")
    server.add_argument("--mac-budget", type=int)
    server.add_argument("--memory-budget-bytes", type=int)
    server.add_argument("--worker-slots", type=int)
    server.add_argument("--max-workers-per-task", type=int)
    server.add_argument("--atol", type=float)
    server.add_argument("--rtol", type=float)
    server.set_defaults(func=cmd_serve)

    check = sub.add_parser("self-check", help="verify snapshot checksums and consistency")
    check.set_defaults(func=cmd_self_check)

    clean = sub.add_parser("clean", help="remove snapshots and cache")
    clean.set_defaults(func=cmd_clean)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        return int(args.func(args, config))
    except DomainError as exc:
        print(
            json.dumps(
                {"ok": False, "code": str(exc.code), "error": exc.as_dict()},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

