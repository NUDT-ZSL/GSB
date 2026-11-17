from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import numpy as np

from .config import ConfigError, load_config, semantic_config_hash
from .consistency import run_consistency_suite
from .dependencies import load_dependencies
from .engine import build_full_result, compare_results, run_incremental
from .injection import run_injection_suite
from .io_utils import (
    atomic_write_json,
    ensure_dir,
    read_json,
    remove_tree,
    require_ascii_paths,
    utc_now_iso,
)
from .plan import load_plan
from .samples import read_jsonl_entries


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic reward-model incremental diagnostics workbench"
    )
    parser.add_argument("--samples-dir", default="sample_data")
    parser.add_argument("--base-data", default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--plan", default=None)
    parser.add_argument("--dependencies", default=None)
    parser.add_argument("--injection-config", default=None)
    parser.add_argument("--state-dir", default="runtime/state")
    parser.add_argument("--report", default="reports/diagnostic_report.json")
    parser.add_argument("--fresh", action="store_true")
    parser.add_argument("--concurrency", type=int, default=None)
    parser.add_argument("--skip-full-comparison", action="store_true")
    parser.add_argument("--skip-injections", action="store_true")
    parser.add_argument("--skip-consistency-suite", action="store_true")
    parser.add_argument(
        "--simulate-crash-after-stage",
        choices=("features", "scores", "summary", "directory", "manifest"),
        default=None,
        help="Test-only fault point for forced-termination recovery validation",
    )
    parser.add_argument(
        "--simulate-batch-delay",
        type=float,
        default=0.0,
        help="Test-only delay inside each batch before its atomic commit",
    )
    parser.add_argument("--json", action="store_true", help="print a compact JSON summary")
    return parser


def _resolve_paths(args: argparse.Namespace) -> dict[str, str]:
    samples = args.samples_dir
    return {
        "base_data": args.base_data or os.path.join(samples, "preferences.jsonl"),
        "config": args.config or os.path.join(samples, "feature_config.json"),
        "plan": args.plan or os.path.join(samples, "batch_plan.json"),
        "dependencies": args.dependencies
        or os.path.join(samples, "dependencies.json"),
        "injection_config": args.injection_config
        or os.path.join(samples, "injection_config.json"),
        "state_dir": args.state_dir,
        "report": args.report,
    }


def _verify_writable(directory: str) -> None:
    ensure_dir(directory)
    descriptor, temporary = tempfile.mkstemp(prefix=".write-check-", dir=directory)
    os.close(descriptor)
    os.unlink(temporary)


def _install_signal_handlers(
    stop_event: threading.Event,
) -> tuple[list[int], dict[str, Any]]:
    received: list[int] = []
    signal_records: dict[str, Any] = {"supported": [], "received": received}
    candidates = ["SIGINT", "SIGTERM", "SIGBREAK"]

    def handler(signum: int, _frame: Any) -> None:
        received.append(signum)
        stop_event.set()

    for candidate in candidates:
        number = getattr(signal, candidate, None)
        if number is None:
            continue
        try:
            signal.signal(number, handler)
            signal_records["supported"].append(candidate)
        except (OSError, ValueError):
            continue
    return received, signal_records


def _snapshot_report(manifest: dict[str, Any]) -> dict[str, Any]:
    return {
        "global_version": manifest["global_version"],
        "cursor": manifest["cursor"],
        "completed_batches": manifest["completed_batches"],
        "config_hash": manifest["config_hash"],
        "feature_config_hash": manifest["feature_config_hash"],
        "data_fingerprint": manifest["data_fingerprint"],
        "version_dir": manifest["version_dir"],
        "stage_versions": manifest["stage_versions"],
        "stages": manifest["stages"],
        "commit_interval": manifest["commit_interval"],
    }


def _build_report(
    *,
    paths: dict[str, str],
    run_result: dict[str, Any],
    comparison: dict[str, Any] | None,
    consistency: dict[str, Any] | None,
    injections: dict[str, Any] | None,
    stopped_early: bool,
    signal_records: dict[str, Any],
    exit_code: int,
) -> dict[str, Any]:
    state = run_result["state"]
    manifest = run_result["manifest"]
    summary = state["summary"]
    batch_events = run_result["events"]
    batch_invariants = all(
        event.get("invariant_checks", {}).get("all_pass", True)
        for event in batch_events
        if event.get("event") in ("batch", "config_refresh", "external_reconcile")
    )
    checks = {
        "snapshot_stage_hashes_verified": bool(
            run_result["snapshot_verification"]["valid"]
        ),
        "batch_invariants_pass": batch_invariants,
        "full_rebuild_identical": (
            comparison["identical"] if comparison is not None else None
        ),
        "consistency_suite_identical": (
            consistency["all_identical"] if consistency is not None else None
        ),
        "injection_suite_all_hit": (
            injections["all_hit"] if injections is not None else None
        ),
        "failure_isolation_present": (
            any(
                item["injection_id"] == "INJ-007" and item["status"] == "hit"
                for item in injections["items"]
            )
            if injections is not None
            else None
        ),
    }
    return {
        "format_version": 1,
        "report_type": "reward_alignment_incremental_diagnostics",
        "generated_at": utc_now_iso(),
        "environment": {
            "python_version": platform.python_version(),
            "numpy_version": np.__version__,
            "platform": platform.platform(),
            "pid": os.getpid(),
        },
        "paths": paths,
        "run": {
            "mode": "incremental",
            "stopped_early": stopped_early,
            "exit_code": exit_code,
            "signals": signal_records,
            "batch_count_completed": len(manifest["completed_batches"]),
            "snapshot_commit_policy": "every_batch_atomic",
        },
        "recovery": run_result["recovery"],
        "snapshot_verification": run_result["snapshot_verification"],
        "snapshot": _snapshot_report(manifest),
        "semantic_config_hash": semantic_config_hash(state["config"]),
        "data_fingerprint": state["data_fingerprint"],
        "batch_events": batch_events,
        "boundary_samples": summary["boundary_samples"],
        "reward_delta_distribution": summary["reward_delta_distribution"],
        "threshold_relation_counts": summary["threshold_relation_counts"],
        "bucket_references": summary["bucket_references"],
        "annotator_similarity_extrema": summary["annotator_groups"]["extrema"],
        "failures": summary["failures"],
        "injection_checks": injections,
        "full_rebuild_comparison": comparison,
        "consistency_checks": consistency,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    paths = _resolve_paths(args)
    try:
        if sys.version_info < (3, 10):
            raise RuntimeError("Python 3.10 or newer is required")
        require_ascii_paths(
            [
                os.path.abspath(paths["base_data"]),
                os.path.abspath(paths["config"]),
                os.path.abspath(paths["plan"]),
                os.path.abspath(paths["dependencies"]),
                os.path.abspath(paths["injection_config"]),
                os.path.abspath(paths["state_dir"]),
                os.path.abspath(paths["report"]),
            ]
        )
        _verify_writable(os.path.dirname(paths["state_dir"]) or ".")
        _verify_writable(os.path.dirname(paths["report"]) or ".")

        if args.fresh:
            remove_tree(paths["state_dir"])
            if os.path.exists(paths["report"]):
                os.unlink(paths["report"])

        config = load_config(paths["config"])
        if args.concurrency is not None:
            if args.concurrency < 1:
                raise ConfigError("E_CONFIG_INVALID", "concurrency must be >= 1")
            config["concurrency"] = args.concurrency
        plan = load_plan(paths["plan"])
        dependencies = load_dependencies(paths["dependencies"])
        base_entries = read_jsonl_entries(paths["base_data"])

        stop_event = threading.Event()
        received_signals, signal_records = _install_signal_handlers(stop_event)
        run_result = run_incremental(
            base_entries=base_entries,
            base_config=config,
            plan=plan,
            dependencies=dependencies,
            state_dir=paths["state_dir"],
            stop_event=stop_event,
            crash_after_stage=args.simulate_crash_after_stage,
            batch_delay_seconds=max(0.0, args.simulate_batch_delay),
        )
        stopped_early = bool(run_result["stopped_early"] or stop_event.is_set())

        completed_cursor = int(run_result["manifest"]["cursor"])
        comparison = None
        if not args.skip_full_comparison:
            final_plan = {
                "format_version": plan.get("format_version", 1),
                "batches": plan["batches"][:completed_cursor],
            }
            full_result = build_full_result(
                base_entries=base_entries,
                base_config=config,
                plan=final_plan,
                dependencies=dependencies,
            )
            comparison = compare_results(run_result["canonical_result"], full_result)

        consistency = None
        if not args.skip_consistency_suite and not stopped_early:
            consistency = run_consistency_suite(
                base_entries=base_entries,
                base_config=config,
                main_plan=plan,
                dependencies=dependencies,
            )

        injections = None
        if not args.skip_injections and not stopped_early:
            injections = run_injection_suite(
                paths["injection_config"], config, dependencies
            )

        exit_code = 0 if not stopped_early else 0
        signal_records["received"] = received_signals
        report = _build_report(
            paths=paths,
            run_result=run_result,
            comparison=comparison,
            consistency=consistency,
            injections=injections,
            stopped_early=stopped_early,
            signal_records=signal_records,
            exit_code=exit_code,
        )
        atomic_write_json(paths["report"], report)
        summary = {
            "status": "ok",
            "snapshot_version": report["snapshot"]["global_version"],
            "batch_cursor": report["snapshot"]["cursor"],
            "completed_batches": report["snapshot"]["completed_batches"],
            "boundary_count": len(report["boundary_samples"]),
            "failure_count": len(report["failures"]),
            "full_rebuild_identical": report["checks"]["full_rebuild_identical"],
            "consistency_suite_identical": report["checks"][
                "consistency_suite_identical"
            ],
            "injection_all_hit": report["checks"]["injection_suite_all_hit"],
            "stopped_early": stopped_early,
            "report": paths["report"],
        }
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        else:
            print(
                "OK",
                f"snapshot={summary['snapshot_version']}",
                f"batches={len(summary['completed_batches'])}",
                f"boundaries={summary['boundary_count']}",
                f"full_equal={summary['full_rebuild_identical']}",
                f"report={paths['report']}",
            )
        return exit_code
    except ConfigError as exc:
        print(
            json.dumps(
                {"status": "error", "business_code": exc.code, "message": exc.message},
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    except BaseException as exc:
        print(
            json.dumps(
                {
                    "status": "error",
                    "business_code": "E_RUN_FAILED",
                    "exception_type": type(exc).__name__,
                    "message": str(exc),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
