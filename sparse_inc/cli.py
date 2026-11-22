"""Command-line examples and acceptance demos."""

from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .batches import seal_batch
from .case import SampleCase, load_cases, parse_case
from .checkpoint import CheckpointStore
from .codes import (
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_CORRUPT,
    E_SNAPSHOT_ROLLBACK,
    E_WRITE_BUSY,
    E_WRITE_CORRUPT,
    E_WRITE_STALE,
    OK,
    exit_code_for,
)
from .config import ComponentConfig
from .errors import BusinessError
from .samples import generate_samples
from .service import SparseIncrementalService


ROOT = Path(__file__).resolve().parents[1]


def _config_from_args(args: argparse.Namespace) -> ComponentConfig:
    return ComponentConfig(
        tolerance_abs=args.abs_tol,
        tolerance_rel=args.rel_tol,
        row_block_size=args.row_block_size,
        max_resident_batches=args.max_resident_batches,
        max_row_buffers=args.max_row_buffers,
        max_parallel_intervals=args.max_parallel_intervals,
        resource_policy=args.resource_policy,
    )


def _find_raw_case(path: str, case_id: str) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            raw = json.loads(line)
            if raw.get("case_id") == case_id:
                return raw
    raise BusinessError(
        business_code="E_CASE_NOT_FOUND",
        case_id=case_id,
        segment="manifest",
        message="case_id is not present in the manifest",
    )


def _apply_writes(
    service: SparseIncrementalService,
    case: SampleCase,
    *,
    crash_after: int | None,
    checkpoint_dir: str | None,
) -> int:
    start = service.next_write_index
    applied_this_run = 0
    for index in range(start, len(case.write_batches)):
        service.submit_batch(case.write_batches[index], batch_index=index)
        applied_this_run += 1
        if crash_after is not None and applied_this_run >= crash_after:
            if checkpoint_dir is None:
                raise RuntimeError("--crash-after requires --checkpoint-dir")
            next_index = index + 1
            pending_payload = {
                "phase": "partial_recompute",
                "next_write_index": next_index,
                "note": "uncommitted side state intentionally left before forced termination",
            }
            CheckpointStore(checkpoint_dir).write_uncommitted_artifact(pending_payload)
            print(
                json.dumps(
                    {
                        "simulated_forced_termination": True,
                        "committed_version": service.ledger.version,
                        "next_write_index": next_index,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            os.kill(os.getpid(), signal.SIGKILL)
    return applied_this_run


def _rejection_batch(
    service: SparseIncrementalService,
    *,
    batch_id: str,
    version: int,
    corrupt: bool,
) -> dict[str, Any]:
    payload = seal_batch(
        {
            "batch_id": batch_id,
            "write_version": version,
            "row_start": 0,
            "row_end": 1,
            "mode": "replace",
            "rows": [
                {
                    "row": 0,
                    "col_indices": [0],
                    "values": [1.25],
                }
            ],
        }
    )
    if corrupt:
        payload["rows"][0]["values"][0] = 2.5
    return payload


def _capture_rejection(
    service: SparseIncrementalService,
    raw: dict[str, Any],
    *,
    expected_code: str,
) -> dict[str, Any]:
    before = service.snapshot_view()
    try:
        service.submit_batch(raw)
        code = OK
        error = None
    except BusinessError as exc:
        code = exc.business_code
        error = exc.to_dict()
    after = service.snapshot_view()
    return {
        "expected_business_code": expected_code,
        "business_code": code,
        "matched_expected": code == expected_code,
        "error": error,
        "ledger_unchanged": (
            before.ledger_snapshot["checksum"] == after.ledger_snapshot["checksum"]
            and before.ledger_snapshot["version"] == after.ledger_snapshot["version"]
        ),
        "result_unchanged": (
            before.result_snapshot["checksum"] == after.result_snapshot["checksum"]
            and before.result_snapshot["version"] == after.result_snapshot["version"]
        ),
    }


def _inject_rejections(service: SparseIncrementalService) -> list[dict[str, Any]]:
    version = service.ledger.version
    stale = _rejection_batch(
        service,
        batch_id="demo_stale_version",
        version=version,
        corrupt=False,
    )
    stale_result = _capture_rejection(service, stale, expected_code=E_WRITE_STALE)
    corrupt = _rejection_batch(
        service,
        batch_id="demo_corrupt_checksum",
        version=version + 1,
        corrupt=True,
    )
    corrupt_result = _capture_rejection(service, corrupt, expected_code=E_WRITE_CORRUPT)
    return [stale_result, corrupt_result]


def _case_report(
    service: SparseIncrementalService,
    *,
    rejections: list[dict[str, Any]] | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    report = service.report()
    report["self_check"] = service.self_check()
    report["rejections"] = rejections or []
    if rejections:
        report["rejection_isolation_ok"] = all(
            item["matched_expected"]
            and item["ledger_unchanged"]
            and item["result_unchanged"]
            for item in rejections
        )
    if extra:
        report.update(extra)
    return report


def _validation_demo(raw: dict[str, Any]) -> list[dict[str, Any]]:
    checks = [
        ("shape_invalid", "E_SHAPE_INVALID", _mutate_shape),
        ("dtype_unsupported", "E_DTYPE_UNSUPPORTED", _mutate_dtype),
        ("csr_indptr_non_monotonic", "E_CSR_STRUCT", _mutate_indptr),
        ("csr_column_out_of_bounds", "E_CSR_STRUCT", _mutate_indices),
        ("nnz_mismatch", "E_NNZ_MISMATCH", _mutate_nnz),
    ]
    results: list[dict[str, Any]] = []
    for name, expected, mutator in checks:
        candidate = copy.deepcopy(raw)
        mutator(candidate)
        try:
            parse_case(candidate)
            code = OK
            error = None
        except BusinessError as exc:
            code = exc.business_code
            error = exc.to_dict()
        results.append(
            {
                "check": name,
                "expected_business_code": expected,
                "business_code": code,
                "matched_expected": code == expected,
                "case_id": error.get("case_id") if error else None,
                "segment": error.get("segment") if error else None,
                "error": error,
            }
        )
    return results


def _mutate_shape(raw: dict[str, Any]) -> None:
    raw["shape_mnk"] = [raw["shape_mnk"][0], raw["shape_mnk"][1], 0]


def _mutate_dtype(raw: dict[str, Any]) -> None:
    raw["dtype"] = "float32"


def _mutate_indptr(raw: dict[str, Any]) -> None:
    indptr = raw["base_csr"]["csr_a"]["indptr"]
    indptr[2] = -1


def _mutate_indices(raw: dict[str, Any]) -> None:
    raw["base_csr"]["csr_a"]["indices"][0] = raw["shape_mnk"][2]


def _mutate_nnz(raw: dict[str, Any]) -> None:
    raw["nnz_a"] = int(raw["nnz_a"]) + 1


def _snapshot_demo(case: SampleCase, config: ComponentConfig) -> dict[str, Any]:
    service = SparseIncrementalService(case, config)
    for index, batch in enumerate(case.write_batches[:2]):
        service.submit_batch(batch, batch_index=index)
    old_bundle = service.export_bundle()
    old_version = service.ledger.version
    for index, batch in enumerate(case.write_batches[2:], start=2):
        service.submit_batch(batch, batch_index=index)
    current_before = service.snapshot_view()
    try:
        service.import_bundle(old_bundle)
        rollback_code = OK
        rollback_error = None
    except BusinessError as exc:
        rollback_code = exc.business_code
        rollback_error = exc.to_dict()
    current_after = service.snapshot_view()

    corrupted = copy.deepcopy(service.export_bundle())
    corrupted["results"]["checksum"] = "sha256:" + "0" * 64
    try:
        service.import_bundle(corrupted)
        corrupt_code = OK
        corrupt_error = None
    except BusinessError as exc:
        corrupt_code = exc.business_code
        corrupt_error = exc.to_dict()
    final = service.snapshot_view()
    return {
        "old_snapshot_version": old_version,
        "current_snapshot_version": current_before.ledger.version,
        "rollback": {
            "expected_business_code": E_SNAPSHOT_ROLLBACK,
            "business_code": rollback_code,
            "matched_expected": rollback_code == E_SNAPSHOT_ROLLBACK,
            "error": rollback_error,
        },
        "corrupt": {
            "expected_business_code": E_SNAPSHOT_CORRUPT,
            "business_code": corrupt_code,
            "matched_expected": corrupt_code == E_SNAPSHOT_CORRUPT,
            "error": corrupt_error,
        },
        "ledger_unchanged": (
            current_before.ledger_snapshot["checksum"]
            == current_after.ledger_snapshot["checksum"]
            == final.ledger_snapshot["checksum"]
        ),
        "result_unchanged": (
            current_before.result_snapshot["checksum"]
            == current_after.result_snapshot["checksum"]
            == final.result_snapshot["checksum"]
        ),
    }


def _resource_demo(case: SampleCase) -> dict[str, Any]:
    strict_config = ComponentConfig(
        row_block_size=2,
        max_row_buffers=1,
        max_parallel_intervals=1,
        resource_policy="strict",
    )
    strict_service = SparseIncrementalService(case, strict_config)
    before_version = strict_service.ledger.version
    try:
        strict_service.submit_batch(case.write_batches[0], batch_index=0)
        strict_code = OK
        strict_error = None
    except BusinessError as exc:
        strict_code = exc.business_code
        strict_error = exc.to_dict()
    strict_after = strict_service.ledger.version

    degrade_config = ComponentConfig(
        row_block_size=2,
        max_row_buffers=1,
        max_parallel_intervals=1,
        resource_policy="degrade",
    )
    degrade_service = SparseIncrementalService(case, degrade_config)
    degraded_result = degrade_service.submit_batch(case.write_batches[0], batch_index=0)
    return {
        "strict": {
            "expected_business_code": E_RESOURCE_LIMIT,
            "business_code": strict_code,
            "matched_expected": strict_code == E_RESOURCE_LIMIT,
            "error": strict_error,
            "ledger_unchanged": before_version == strict_after == 0,
        },
        "degrade": {
            "business_code": degraded_result["business_code"],
            "degraded": degraded_result["degraded"],
            "degradation_categories": degraded_result["degradation_categories"],
            "accepted_write_version": degraded_result["write_version"],
        },
    }


def _concurrency_demo(case: SampleCase) -> dict[str, Any]:
    service = SparseIncrementalService(case)
    entered = threading.Event()
    release = threading.Event()

    def hold_writer() -> None:
        with service._mutex:
            entered.set()
            release.wait(timeout=5)

    holder = threading.Thread(target=hold_writer, name="demo-held-writer")
    holder.start()
    if not entered.wait(timeout=2):
        raise RuntimeError("failed to acquire demonstration writer guard")
    started = time.monotonic()
    try:
        service.submit_batch(case.write_batches[0], batch_index=0)
        code = OK
        error = None
    except BusinessError as exc:
        code = exc.business_code
        error = exc.to_dict()
    elapsed = time.monotonic() - started
    release.set()
    holder.join(timeout=2)
    return {
        "expected_business_code": E_WRITE_BUSY,
        "business_code": code,
        "matched_expected": code == E_WRITE_BUSY,
        "returned_without_waiting_for_writer": elapsed < 1.0,
        "observed_latency_ms": round(elapsed * 1000.0, 3),
        "error": error,
        "ledger_unchanged": service.ledger.version == 0,
    }


def _run_subprocess(command: list[str]) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.run(
        command,
        cwd=str(ROOT),
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _recovery_demo(
    manifest: str,
    case_id: str,
    reports_dir: Path,
    runs_dir: Path,
) -> dict[str, Any]:
    full_run = runs_dir / "recovery_full"
    crash_run = runs_dir / "recovery_crash"
    for path in (full_run, crash_run):
        if path.exists():
            shutil.rmtree(path)
    full_report_path = reports_dir / "recovery_full.json"
    resumed_report_path = reports_dir / "recovery_resumed.json"
    base_command = [
        sys.executable,
        "-m",
        "sparse_inc.cli",
        "run-case",
        "--manifest",
        manifest,
        "--case-id",
        case_id,
    ]
    full = _run_subprocess(
        base_command
        + [
            "--checkpoint-dir",
            str(full_run),
            "--report",
            str(full_report_path),
            "--quiet",
        ]
    )
    if full.returncode != 0:
        raise RuntimeError(f"full run failed: {full.stderr or full.stdout}")

    crashed = _run_subprocess(
        base_command
        + [
            "--checkpoint-dir",
            str(crash_run),
            "--crash-after",
            "2",
            "--quiet",
        ]
    )
    pending_path = crash_run / "checkpoint.json.pending"
    forced_termination_observed = crashed.returncode in {-signal.SIGKILL, 128 + signal.SIGKILL}
    pending_existed = pending_path.exists()

    resumed = _run_subprocess(
        base_command
        + [
            "--checkpoint-dir",
            str(crash_run),
            "--resume",
            "--report",
            str(resumed_report_path),
            "--quiet",
        ]
    )
    if resumed.returncode != 0:
        raise RuntimeError(
            "resume run failed: "
            + (resumed.stderr or resumed.stdout)
            + "\ncrash stderr: "
            + crashed.stderr
        )
    full_checkpoint = (full_run / "checkpoint.json").read_bytes()
    resumed_checkpoint = (crash_run / "checkpoint.json").read_bytes()
    full_case_report = json.loads(full_report_path.read_text(encoding="utf-8"))
    resumed_case_report = json.loads(resumed_report_path.read_text(encoding="utf-8"))
    pending_removed = not pending_path.exists()
    checkpoint_bytes_equal = full_checkpoint == resumed_checkpoint
    ledger_equal = (
        full_case_report["write_ledger"] == resumed_case_report["write_ledger"]
    )
    result_equal = (
        full_case_report["result_summary"] == resumed_case_report["result_summary"]
    )
    snapshot_equal = full_case_report["snapshot"] == resumed_case_report["snapshot"]
    return {
        "forced_termination": {
            "process_return_code": crashed.returncode,
            "sigkill_observed": forced_termination_observed,
            "uncommitted_artifact_left": pending_existed,
            "complete_exit_report_generated": False,
            "note": "SIGKILL intentionally provides no graceful exit report",
        },
        "resume": {
            "business_code": resumed_case_report["business_code"],
            "discarded_pending": resumed_case_report["recovery"]["discarded_pending"],
            "pending_artifact_removed": pending_removed,
            "loaded_version": resumed_case_report["recovery"]["loaded_version"],
            "loaded_next_write_index": resumed_case_report["recovery"][
                "loaded_next_write_index"
            ],
        },
        "equivalence": {
            "checkpoint_bytes_equal": checkpoint_bytes_equal,
            "ledger_equal": ledger_equal,
            "result_equal": result_equal,
            "snapshot_envelopes_equal": snapshot_equal,
            "full_checkpoint_checksum": json.loads(full_checkpoint)["bundle_checksum"],
            "resumed_checkpoint_checksum": json.loads(resumed_checkpoint)["bundle_checksum"],
        },
        "full_report": str(full_report_path),
        "resumed_report": str(resumed_report_path),
    }


def command_generate(args: argparse.Namespace) -> int:
    paths = generate_samples(args.out, seed=args.seed)
    print(json.dumps(paths, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_run_case(args: argparse.Namespace) -> int:
    raw = _find_raw_case(args.manifest, args.case_id)
    case = parse_case(raw, operation_override=args.operation)
    config = _config_from_args(args)
    if args.crash_after is not None and args.checkpoint_dir is None:
        raise BusinessError(
            business_code="E_INVALID_REQUEST",
            case_id=case.case_id,
            segment="--crash-after",
            message="--crash-after requires --checkpoint-dir",
        )
    service = SparseIncrementalService(
        case,
        config,
        checkpoint_dir=args.checkpoint_dir,
        resume=args.resume,
    )
    _apply_writes(
        service,
        case,
        crash_after=args.crash_after,
        checkpoint_dir=args.checkpoint_dir,
    )
    rejections = _inject_rejections(service) if args.inject_rejections else []
    report = _case_report(service, rejections=rejections)
    if args.report:
        report_path = Path(args.report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if not args.quiet:
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


def command_demo(args: argparse.Namespace) -> int:
    samples_dir = Path(args.samples)
    reports_dir = Path(args.reports)
    runs_dir = Path(args.runs)
    reports_dir.mkdir(parents=True, exist_ok=True)
    runs_dir.mkdir(parents=True, exist_ok=True)
    paths = generate_samples(samples_dir, seed=args.seed)
    cases = load_cases(paths["manifest"])
    spmm_case = cases["spmm_inc_demo"]
    spspmm_case = cases["spspmm_inc_demo"]
    config = ComponentConfig()

    spmm_service = SparseIncrementalService(
        spmm_case,
        config,
        checkpoint_dir=str(runs_dir / "spmm_demo"),
    )
    _apply_writes(
        spmm_service,
        spmm_case,
        crash_after=None,
        checkpoint_dir=str(runs_dir / "spmm_demo"),
    )
    spmm_rejections = _inject_rejections(spmm_service)
    spmm_report = _case_report(spmm_service, rejections=spmm_rejections)
    _write_json(reports_dir / "spmm_case_report.json", spmm_report)

    spspmm_service = SparseIncrementalService(
        spspmm_case,
        config,
        checkpoint_dir=str(runs_dir / "spspmm_demo"),
    )
    _apply_writes(
        spspmm_service,
        spspmm_case,
        crash_after=None,
        checkpoint_dir=str(runs_dir / "spspmm_demo"),
    )
    spspmm_report = _case_report(spspmm_service)
    _write_json(reports_dir / "spspmm_case_report.json", spspmm_report)

    validation = _validation_demo(_find_raw_case(paths["manifest"], "spmm_inc_demo"))
    snapshot = _snapshot_demo(spmm_case, config)
    resources = _resource_demo(spmm_case)
    concurrency = _concurrency_demo(spmm_case)
    recovery = _recovery_demo(
        paths["manifest"],
        "spmm_inc_demo",
        reports_dir,
        runs_dir,
    )
    component_checks = {
        "business_code": OK,
        "validation_codes": validation,
        "snapshot_version_guards": snapshot,
        "resource_policy": resources,
        "concurrency_conflict": concurrency,
    }
    _write_json(reports_dir / "component_checks.json", component_checks)
    _write_json(reports_dir / "recovery_demo.json", recovery)

    all_checks = (
        [item["matched_expected"] for item in validation]
        + [
            snapshot["rollback"]["matched_expected"],
            snapshot["corrupt"]["matched_expected"],
            snapshot["ledger_unchanged"],
            snapshot["result_unchanged"],
            resources["strict"]["matched_expected"],
            resources["strict"]["ledger_unchanged"],
            concurrency["matched_expected"],
            concurrency["returned_without_waiting_for_writer"],
            concurrency["ledger_unchanged"],
            recovery["forced_termination"]["sigkill_observed"],
            recovery["forced_termination"]["uncommitted_artifact_left"],
            recovery["resume"]["discarded_pending"],
            recovery["resume"]["pending_artifact_removed"],
            recovery["equivalence"]["checkpoint_bytes_equal"],
            recovery["equivalence"]["ledger_equal"],
            recovery["equivalence"]["result_equal"],
            recovery["equivalence"]["snapshot_envelopes_equal"],
            spmm_report["self_check"]["business_code"] == OK,
            spspmm_report["self_check"]["business_code"] == OK,
            spmm_report["rejection_isolation_ok"],
            spspmm_report["deviation"]["within_tolerance"],
        ]
    )
    passed = all(bool(item) for item in all_checks)
    summary = {
        "business_code": OK if passed else "E_ACCEPTANCE_FAILED",
        "passed": passed,
        "checks": len(all_checks),
        "checks_passed": sum(1 for item in all_checks if item),
        "samples": paths,
        "reports": {
            "spmm": str(reports_dir / "spmm_case_report.json"),
            "spspmm": str(reports_dir / "spspmm_case_report.json"),
            "component_checks": str(reports_dir / "component_checks.json"),
            "recovery": str(reports_dir / "recovery_demo.json"),
        },
        "headline": {
            "spmm_max_abs_error": spmm_report["deviation"]["max_abs_error"],
            "spspmm_max_abs_error": spspmm_report["deviation"]["max_abs_error"],
            "spmm_untouched_blocks": spmm_report["self_check"]["untouched_checksums"][
                "blocks_checked"
            ],
            "spspmm_untouched_blocks": spspmm_report["self_check"][
                "untouched_checksums"
            ]["blocks_checked"],
            "rejected_batches": [
                {
                    "business_code": item["business_code"],
                    "ledger_unchanged": item["ledger_unchanged"],
                }
                for item in spmm_rejections
            ],
            "recovery_checkpoint_bytes_equal": recovery["equivalence"][
                "checkpoint_bytes_equal"
            ],
        },
    }
    _write_json(reports_dir / "demo_report.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if passed else 1


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Incremental CSR SpMM/SpMSpM component")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate_parser = subparsers.add_parser("generate")
    generate_parser.add_argument("--out", default="samples")
    generate_parser.add_argument("--seed", type=int, default=20261123)
    generate_parser.set_defaults(func=command_generate)

    run_parser = subparsers.add_parser("run-case")
    run_parser.add_argument("--manifest", default="samples/cases.jsonl")
    run_parser.add_argument("--case-id", required=True)
    run_parser.add_argument("--operation", choices=["spmm", "spspmm"])
    run_parser.add_argument("--checkpoint-dir")
    run_parser.add_argument("--resume", action="store_true")
    run_parser.add_argument("--crash-after", type=int)
    run_parser.add_argument("--inject-rejections", action="store_true")
    run_parser.add_argument("--report")
    run_parser.add_argument("--quiet", action="store_true")
    _add_config_arguments(run_parser)
    run_parser.set_defaults(func=command_run_case)

    demo_parser = subparsers.add_parser("demo")
    demo_parser.add_argument("--samples", default="samples")
    demo_parser.add_argument("--reports", default="reports")
    demo_parser.add_argument("--runs", default="runs")
    demo_parser.add_argument("--seed", type=int, default=20261123)
    demo_parser.set_defaults(func=command_demo)
    return parser


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--abs-tol", type=float, default=1e-11)
    parser.add_argument("--rel-tol", type=float, default=1e-11)
    parser.add_argument("--row-block-size", type=int, default=4)
    parser.add_argument("--max-resident-batches", type=int, default=4)
    parser.add_argument("--max-row-buffers", type=int, default=4)
    parser.add_argument("--max-parallel-intervals", type=int, default=4)
    parser.add_argument(
        "--resource-policy",
        choices=["degrade", "strict"],
        default="degrade",
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except BusinessError as exc:
        print(
            json.dumps(exc.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
            file=sys.stderr,
        )
        return exc.exit_code
    except Exception as exc:
        print(
            json.dumps(
                {
                    "business_code": "E_INTERNAL",
                    "message": str(exc),
                    "exception_type": type(exc).__name__,
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
