from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from .compare import compare_ledger_documents
from .canonical import signature
from .engine import WritePathEngine, normalize_policy
from .errors import DiagnosticError, ErrorCode
from .ledger import Ledger
from .seed import FIXED_SEED, generate_fixtures
from .snapshots import build_unified_view, deterministic_view_fingerprint
from .validation import load_fixtures


DEFAULT_FIXTURES = Path("fixtures") / "seed"
DEFAULT_STATE = Path("diagnostic_workspace")


def print_json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False))


def policy_dir(state_dir: str | Path, policy: str) -> Path:
    canonical = normalize_policy(policy)
    return Path(state_dir) / canonical


def attach_snapshot_view(engine: WritePathEngine, bundle: Any) -> None:
    view = build_unified_view(bundle)
    fingerprint = deterministic_view_fingerprint(bundle)

    def mutate(doc: dict[str, Any], connection: Any) -> None:
        doc["snapshot_decision"] = view
        doc["clock_fingerprint"] = fingerprint

    clock = max((event["logical_clock"] for event in bundle.events), default=0)
    engine.ledger.update(mutate, logical_clock=clock)


def cmd_seed(args: argparse.Namespace) -> dict[str, Any]:
    path = generate_fixtures(args.output, seed=args.seed, clean=not args.no_clean)
    bundle = load_fixtures(path)
    return {
        "ok": True,
        "command": "seed",
        "output": str(path),
        "seed": bundle.seed,
        "object_count": len(bundle.objects),
        "event_count": len(bundle.events),
        "injection_case_count": len(bundle.injections),
        "v2_shard_count": len(bundle.v2_shards),
    }


def cmd_load(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_fixtures(args.fixtures)
    view = build_unified_view(bundle)
    fingerprint = deterministic_view_fingerprint(bundle)
    return {
        "ok": True,
        "command": "load",
        "fixtures": str(bundle.root),
        "summary": {
            "object_count": len(bundle.objects),
            "event_count": len(bundle.events),
            "injection_case_count": len(bundle.injections),
            "v1_record_count": len(bundle.v1_records),
            "v2_record_count": len(bundle.v2_records),
            "v2_shard_count": len(bundle.v2_shards),
        },
        "clock_fingerprint": fingerprint,
        "unified_snapshot_view": view,
    }


def _run_one_policy(args: argparse.Namespace, bundle: Any, policy: str, *, fault_case=None,
                    hard_kill: bool = False, worker_id: str | None = None) -> WritePathEngine:
    engine = WritePathEngine(
        bundle,
        policy_dir(args.state_dir, policy),
        policy=policy,
        run_id=args.run_id,
        worker_id=worker_id or args.worker_id or f"worker-{normalize_policy(policy)}",
        fault_case=fault_case,
        hard_kill=hard_kill,
    )
    attach_snapshot_view(engine, bundle)
    return engine


def _spawn_parallel(args: argparse.Namespace, policies: list[str]) -> dict[str, Any]:
    worker_results = []
    for policy in policies:
        processes = []
        for index in range(args.workers):
            command = [
                sys.executable, "-m", "diagnostic_engine.cli", "run",
                "--fixtures", args.fixtures,
                "--state-dir", args.state_dir,
                "--run-id", args.run_id,
                "--policy", policy,
                "--shard-index", str(index),
                "--shard-count", str(args.workers),
                "--worker-id", f"worker-{policy}-{index}",
            ]
            processes.append((index, subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)))
        for index, process in processes:
            stdout, stderr = process.communicate()
            try:
                payload = json.loads(stdout)
            except json.JSONDecodeError:
                payload = {"ok": False, "stdout": stdout, "stderr": stderr}
            worker_results.append({
                "policy": policy,
                "shard_index": index,
                "return_code": process.returncode,
                "payload": payload,
            })
    return {"ok": all(item["return_code"] == 0 for item in worker_results), "workers": worker_results}


def cmd_run(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_fixtures(args.fixtures)
    if args.workers and args.workers > 1 and args.shard_index is None:
        policies = ["metadata_first", "write_log"] if args.policy == "both" else [args.policy]
        parallel = _spawn_parallel(args, policies)
        return {"ok": parallel["ok"], "command": "run", "parallel": parallel}

    policies = ["metadata_first", "write_log"] if args.policy == "both" else [args.policy]
    outputs: dict[str, Any] = {}
    documents: dict[str, dict[str, Any]] = {}
    for policy in policies:
        engine = _run_one_policy(args, bundle, policy)
        result = engine.run_events(
            shard_index=args.shard_index,
            shard_count=args.shard_count,
        )
        outputs[normalize_policy(policy)] = {
            "processed": result["processed"],
            "worker_id": engine.worker_id,
            "state_dir": str(engine.state_dir),
        }
        documents[normalize_policy(policy)] = engine.ledger.read()

    comparison = None
    if len(documents) == 2:
        comparison = compare_ledger_documents(
            documents["metadata_first"], documents["write_log"],
            left_name="metadata_first", right_name="write_log",
        )
    return {
        "ok": True,
        "command": "run",
        "state_dir": args.state_dir,
        "policies": outputs,
        "comparison": comparison,
        "ledgers": documents if args.include_ledger else None,
    }


def _resolve_injection(args: argparse.Namespace, bundle: Any) -> tuple[dict[str, Any], str]:
    if args.case:
        case = bundle.injection_case(args.case)
        policy = args.policy or case["policy"]
        return case, policy
    if not args.event_seq or not args.stage or not args.fault_type:
        raise DiagnosticError(
            ErrorCode.CLI_INVALID_ARGUMENT,
            "ad hoc injection requires --event-seq, --stage and --fault-type",
        )
    event = bundle.event_by_seq[args.event_seq]
    policy = args.policy or "metadata_first"
    case = {
        "injection_id": args.injection_id or f"ADHOC-{args.event_seq}-{args.stage}-{args.fault_type}",
        "event_seq": args.event_seq,
        "policy": policy,
        "stage": args.stage,
        "fault_type": args.fault_type,
        "hard": args.hard_kill,
    }
    return case, policy


def cmd_inject(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_fixtures(args.fixtures)
    case, policy = _resolve_injection(args, bundle)
    hard_kill = bool(args.hard_kill) and not bool(args.safe)
    engine = _run_one_policy(
        args, bundle, policy,
        fault_case=case, hard_kill=hard_kill,
        worker_id=args.worker_id or f"injector-{case['injection_id']}",
    )
    target_seq = int(case["event_seq"])
    engine.run_events(stop_before_seq=target_seq)
    target_event = bundle.event_by_seq[target_seq]
    try:
        engine.process_event(target_event)
    except DiagnosticError as error:
        return {
            "ok": False,
            "command": "inject",
            "injection": case,
            "hard_kill": hard_kill,
            "state_dir": args.state_dir,
            "policy_dir": str(engine.state_dir),
            "resume_command": (
                f"python -m diagnostic_engine.cli resume --fixtures {args.fixtures} "
                f"--state-dir {args.state_dir} --case {case['injection_id']}"
            ),
            "error": error.evidence(),
            "ledger": engine.ledger.read() if args.include_ledger else None,
        }
    return {
        "ok": True,
        "command": "inject",
        "injection": case,
        "state_dir": args.state_dir,
        "policy_dir": str(engine.state_dir),
        "ledger": engine.ledger.read() if args.include_ledger else None,
    }


def cmd_resume(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_fixtures(args.fixtures)
    case = None
    event_seq = args.event_seq
    policy = args.policy or "metadata_first"
    if args.case:
        case = bundle.injection_case(args.case)
        policy = args.policy or case["policy"]
        event_seq = event_seq or int(case["event_seq"])
    engine = _run_one_policy(
        args, bundle, policy,
        worker_id=args.worker_id or f"recovery-{normalize_policy(policy)}",
    )
    result = engine.resume(seq=event_seq)
    invariants = engine.ledger.verify_invariants()
    return {
        "ok": invariants["ok"],
        "command": "resume",
        "case": case,
        "policy_dir": str(engine.state_dir),
        "recovery": {k: v for k, v in result.items() if k != "ledger"},
        "invariants": invariants,
        "ledger": result["ledger"] if args.include_ledger else None,
    }


def _status_for_policy(args: argparse.Namespace, policy: str) -> dict[str, Any]:
    directory = policy_dir(args.state_dir, policy)
    db_path = directory / "ledger.sqlite"
    if not db_path.exists():
        return {"exists": False, "policy_dir": str(directory)}
    ledger = Ledger(directory, run_id=args.run_id, policy=policy, worker_id="status")
    document = ledger.read()
    invariants = ledger.verify_invariants()
    return {
        "exists": True,
        "policy_dir": str(directory),
        "invariants": invariants,
        "status": document.get("status"),
        "ledger_version": document.get("ledger_version"),
        "seq_watermark": document.get("seq_watermark"),
        "clock_watermark": document.get("clock_watermark"),
        "completed_seqs": document.get("completed_seqs"),
        "pending_seqs": sorted(document.get("pending_operations", {}), key=int),
        "cache": document.get("cache"),
        "orphans": document.get("orphans"),
        "dirty": document.get("dirty"),
        "counts": document.get("counts"),
        "replay_watermark": document.get("replay_watermark"),
        "ledger": document if args.include_ledger else None,
    }


def cmd_status(args: argparse.Namespace) -> dict[str, Any]:
    policies = ["metadata_first", "write_log"] if args.policy == "both" else [args.policy]
    states = {normalize_policy(policy): _status_for_policy(args, policy) for policy in policies}
    return {"ok": True, "command": "status", "state_dir": args.state_dir, "policies": states}


def cmd_verify(args: argparse.Namespace) -> dict[str, Any]:
    bundle = load_fixtures(args.fixtures)
    fingerprint = deterministic_view_fingerprint(bundle)
    checks: dict[str, Any] = {
        "fixtures_valid": True,
        "snapshot_repeat_identical": fingerprint["identical_on_repeat"],
    }
    policy_states: dict[str, Any] = {}
    documents: dict[str, dict[str, Any]] = {}
    for policy in ("metadata_first", "write_log", "direct_reversed"):
        directory = policy_dir(args.state_dir, policy)
        if (directory / "ledger.sqlite").exists():
            status = _status_for_policy(args, policy)
            policy_states[policy] = status
            if status.get("exists"):
                ledger = Ledger(
                    directory, run_id=args.run_id, policy=policy, worker_id="verify"
                )
                documents[policy] = ledger.read()
                checks[f"{policy}_ledger_invariants"] = status["invariants"]["ok"]
                document = documents[policy]
                filesystem_ok = True
                filesystem_errors: list[dict[str, Any]] = []
                for key, entry in sorted(document.get("cache", {}).items()):
                    if entry.get("state") != "clean" or not entry.get("data_present"):
                        continue
                    data_path = entry.get("data_path")
                    path = directory / str(data_path)
                    if not path.is_file():
                        filesystem_ok = False
                        filesystem_errors.append({"object_key": key, "problem": "missing_clean_data", "path": data_path})
                        continue
                    actual_signature = signature(path.read_bytes())
                    if actual_signature != entry.get("expected_signature"):
                        filesystem_ok = False
                        filesystem_errors.append({"object_key": key, "problem": "signature_mismatch"})
                checks[f"{policy}_filesystem_cache"] = filesystem_ok
                checks[f"{policy}_no_uncommitted_pending"] = not document.get("pending_operations")
                if filesystem_errors:
                    policy_states[policy]["filesystem_errors"] = filesystem_errors

    comparison = None
    if "metadata_first" in documents and "write_log" in documents:
        comparison = compare_ledger_documents(
            documents["metadata_first"], documents["write_log"]
        )
        checks["policy_comparison_consistent"] = comparison["consistent"]

    ok = all(value is True for value in checks.values())
    if args.expect_consistent and comparison is not None and not comparison["consistent"]:
        ok = False
    return {
        "ok": ok,
        "command": "verify",
        "fixtures": str(bundle.root),
        "checks": checks,
        "clock_fingerprint": fingerprint,
        "comparison": comparison,
        "policy_status": policy_states if args.include_ledger else None,
    }


class JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        payload = {"ok": False, "error": {"code": ErrorCode.CLI_INVALID_ARGUMENT, "message": message}}
        print_json(payload)
        raise SystemExit(2)


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fixtures", default=argparse.SUPPRESS)
    common.add_argument("--state-dir", default=argparse.SUPPRESS)
    common.add_argument("--run-id", default=argparse.SUPPRESS)
    parser = JSONArgumentParser(description="Crash-consistency diagnosis engine", parents=[common])
    parser.set_defaults(
        fixtures=str(DEFAULT_FIXTURES),
        state_dir=str(DEFAULT_STATE),
        run_id="fixed-seed-run",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    seed = sub.add_parser("seed", help="generate deterministic fixtures", parents=[common])
    seed.add_argument("--output", default=str(DEFAULT_FIXTURES))
    seed.add_argument("--seed", type=int, default=FIXED_SEED)
    seed.add_argument("--no-clean", action="store_true")
    seed.set_defaults(handler=cmd_seed)

    load = sub.add_parser("load", help="parse and validate all fixtures", parents=[common])
    load.set_defaults(handler=cmd_load)

    run = sub.add_parser("run", help="run one or both write paths", parents=[common])
    run.add_argument("--policy", choices=["metadata-first", "metadata_first", "write-log", "write_log",
                                          "direct-reversed", "direct_reversed", "both"], default="both")
    run.add_argument("--shard-index", type=int)
    run.add_argument("--shard-count", type=int)
    run.add_argument("--workers", type=int, default=1)
    run.add_argument("--worker-id")
    run.add_argument("--include-ledger", action="store_true")
    run.set_defaults(handler=cmd_run)

    inject = sub.add_parser("inject", help="inject a deterministic fault", parents=[common])
    inject.add_argument("--case")
    inject.add_argument("--event-seq", type=int)
    inject.add_argument("--stage")
    inject.add_argument("--fault-type", choices=["force_kill", "write_interrupt", "generation_rollback", "log_lost"])
    inject.add_argument("--injection-id")
    inject.add_argument("--policy", choices=["metadata-first", "metadata_first", "write-log", "write_log",
                                             "direct-reversed", "direct_reversed"])
    inject.add_argument("--hard-kill", action="store_true", help="actually call os._exit(137) at kill point")
    inject.add_argument("--safe", action="store_true", help="never hard kill; raise a simulated kill")
    inject.add_argument("--worker-id")
    inject.add_argument("--include-ledger", action="store_true")
    inject.set_defaults(handler=cmd_inject)

    resume = sub.add_parser("resume", help="recover pending half-written state", parents=[common])
    resume.add_argument("--case")
    resume.add_argument("--event-seq", type=int)
    resume.add_argument("--policy", choices=["metadata-first", "metadata_first", "write-log", "write_log",
                                             "direct-reversed", "direct_reversed"])
    resume.add_argument("--worker-id")
    resume.add_argument("--include-ledger", action="store_true")
    resume.set_defaults(handler=cmd_resume)

    status = sub.add_parser("status", help="show atomic ledger status", parents=[common])
    status.add_argument("--policy", choices=["metadata-first", "metadata_first", "write-log", "write_log",
                                             "direct-reversed", "direct_reversed", "both"], default="both")
    status.add_argument("--include-ledger", action="store_true")
    status.set_defaults(handler=cmd_status)

    verify = sub.add_parser("verify", help="validate fixtures, ledger invariants and path consistency", parents=[common])
    verify.add_argument("--expect-consistent", action="store_true")
    verify.add_argument("--include-ledger", action="store_true")
    verify.set_defaults(handler=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    # Accept hyphenated CLI policy names while preserving stable underscore IDs.
    if hasattr(args, "policy") and args.policy and args.policy != "both":
        args.policy = args.policy.replace("-", "_")
    try:
        payload = args.handler(args)
    except DiagnosticError as error:
        print_json({"ok": False, "command": getattr(args, "command", None), "error": error.evidence()})
        return 2
    print_json(payload)
    if isinstance(payload, dict) and payload.get("ok") is False:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
