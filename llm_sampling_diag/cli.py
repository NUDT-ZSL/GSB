from __future__ import annotations

import argparse
import json
import sys
import threading
from pathlib import Path
from typing import Any

from .canonical import pretty_json, sha256_json
from .diagnostics import diagnose
from .errors import DiagnosticError
from .formats import dispatch_document, read_suite
from .migrate import MigrationPolicy, migrate_task, validate_migration_roundtrip
from .model import OperatorConfig
from .pipeline import run_task
from .samplegen import generate_suite
from .selfcheck import run_self_check
from .snapshots import SnapshotManager


def _config_from_args(args: argparse.Namespace) -> OperatorConfig:
    return OperatorConfig(
        mask_priority=args.mask_priority,
        dtype=args.dtype,
        rounding_mode=args.rounding_mode,
        subnormal_guard=args.subnormal_guard,
        tolerance=args.tolerance,
        rng_algorithm=args.rng_algorithm,
    )


def _add_config_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mask-priority", default="mask_first", choices=["mask_first", "k_first"])
    parser.add_argument("--dtype", default="float32", choices=["float32", "float64"])
    parser.add_argument("--rounding-mode", default="none", choices=["none", "nearest_even", "toward_zero", "floor", "ceil"])
    parser.add_argument("--subnormal-guard", default="clamp_tiny", choices=["none", "clamp_tiny", "flush_zero"])
    parser.add_argument("--tolerance", type=float, default=1e-5)
    parser.add_argument("--rng-algorithm", default="pcg64", choices=["pcg64"])


def _write_json(path: str | Path, payload: Any) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(pretty_json(payload), encoding="utf-8")
    return target


def cmd_generate(args: argparse.Namespace) -> int:
    manifest = generate_suite(args.out, seed=args.seed)
    print(
        json.dumps(
            {
                "status": "ok",
                "suite": str(Path(args.out).resolve()),
                "task_count": manifest["task_count"],
                "case_ids": manifest["case_ids"],
                "logical_checksum": manifest["logical_checksum"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def cmd_selfcheck(args: argparse.Namespace) -> int:
    config = _config_from_args(args)
    report = run_self_check(args.suite, config=config)
    _write_json(args.out, report)
    print(
        json.dumps(
            {
                "status": "ok",
                "report": str(Path(args.out).resolve()),
                "summary": report["summary"],
                "report_checksum": report["report_checksum"],
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "sample_sequences": case["sample_sequences"],
                        "max_abs_error": case["max_abs_error"],
                    }
                    for case in report["cases"]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def _migration_tour(suite_root: Path) -> dict[str, Any]:
    suite = read_suite(suite_root)
    v3_task = suite.per_format["v3"].tasks[0]
    v1_task = suite.per_format["v1"].tasks[0]
    incremental = dispatch_document(suite_root / "v3" / "incremental_rev2.json").tasks[0]
    roundtrips: dict[str, dict[str, bool]] = {}
    for task in suite.per_format["v3"].tasks:
        roundtrips[task.case_id] = {
            target: validate_migration_roundtrip(task, target) for target in ("v1", "v2", "v3")
        }
    flatten = migrate_task(v3_task, "v1", policy=MigrationPolicy(mask_policy="flatten"))
    v2 = migrate_task(v3_task, "v2")
    v3 = migrate_task(v1_task, "v3")
    revision_loss = migrate_task(incremental, "v1")
    try:
        migrate_task(incremental, "v1", policy=MigrationPolicy(strict=True))
        strict_error = None
    except DiagnosticError as error:
        strict_error = error.to_dict()
    return {
        "roundtrip_results": roundtrips,
        "v3_to_v1_flatten": {
            "semantic_complete": flatten.semantic_complete,
            "record": flatten.records[0],
            "payload_mask": flatten.payload.get("mask_excluded_indices"),
        },
        "v3_to_v2": {
            "semantic_complete": v2.semantic_complete,
            "record": v2.records[0],
        },
        "v1_to_v3_upgrade": {
            "semantic_complete": v3.semantic_complete,
            "record": v3.records[0],
        },
        "incremental_revision_to_v1": {
            "semantic_complete": revision_loss.semantic_complete,
            "record": revision_loss.records[0],
        },
        "strict_policy_error": strict_error,
    }


def _snapshot_tour(suite_root: Path, config: OperatorConfig) -> dict[str, Any]:
    suite = read_suite(suite_root)
    task = suite.per_format["v3"].tasks[0]
    result = run_task(task, config)
    manager = SnapshotManager()
    version_one = manager.commit_bundle(
        version=1,
        task_payload=task.to_snapshot_dict(),
        parameter_payload=config.to_dict(),
        result_payload={
            "case_id": result["case_id"],
            "candidate_order": result["candidate_order"],
            "sample_sequence": result["sample_sequence"],
            "result_checksum": result["result_checksum"],
        },
    )
    version_two = manager.commit_bundle(
        version=2,
        task_payload=task.to_snapshot_dict(),
        parameter_payload={**config.to_dict(), "tolerance": config.tolerance / 10.0},
        result_payload={
            "case_id": result["case_id"],
            "candidate_order": result["candidate_order"],
            "sample_sequence": [result["sample_sequence"][0]],
            "result_checksum": "replaced-" + result["result_checksum"][:16],
        },
    )
    try:
        manager.commit("result", 1, {"attempt": "rollback"})
        rollback_error = None
    except DiagnosticError as error:
        rollback_error = error.to_dict()
    entered = threading.Event()
    busy_writer = SnapshotManager(hook=lambda kind: entered.set())
    writer_errors: list[dict[str, Any]] = []

    def writer() -> None:
        try:
            busy_writer.commit("result", 1, {"value": [1, 2, 3]}, commit_delay_s=0.15)
        except DiagnosticError as error:
            writer_errors.append(error.to_dict())

    thread = threading.Thread(target=writer)
    thread.start()
    entered.wait(timeout=1.0)
    try:
        busy_writer.commit("result", 1, {"value": [1, 2, 3]})
        busy_error = None
    except DiagnosticError as error:
        busy_error = error.to_dict()
    thread.join(timeout=2.0)
    return {
        "version_one": version_one,
        "version_two": version_two,
        "current_result": manager.get("result").to_dict(),
        "rollback_error": rollback_error,
        "concurrent_busy_error": busy_error,
        "concurrent_writer_errors": writer_errors,
        "history_versions": [snapshot["version"] for snapshot in manager.history("result")],
    }


def _error_code_tour(suite_root: Path, config: OperatorConfig) -> dict[str, Any]:
    outcomes: dict[str, Any] = {}
    probes = {
        "unknown_version": lambda: dispatch_document(suite_root / "invalid" / "unknown_version.json"),
        "duplicate_case": lambda: dispatch_document(suite_root / "invalid" / "duplicate_case_v1.json"),
        "missing_seed": lambda: dispatch_document(suite_root / "invalid" / "missing_seed_v1.json"),
        "shape_mismatch": lambda: dispatch_document(suite_root / "invalid" / "shape_mismatch_v3.json"),
        "v2_timestamp_regression": lambda: dispatch_document(suite_root / "invalid" / "v2_timestamp_regression"),
        "v2_metadata_mismatch": lambda: dispatch_document(suite_root / "invalid" / "v2_metadata_mismatch"),
    }
    for name, probe in probes.items():
        try:
            probe()
            outcomes[name] = {"error_code": None}
        except DiagnosticError as error:
            outcomes[name] = error.to_dict()
    suite = read_suite(suite_root)
    task = suite.per_format["v1"].tasks[0]
    try:
        diagnose(task, OperatorConfig(
            mask_priority=config.mask_priority,
            dtype="float32",
            rounding_mode="none",
            subnormal_guard="clamp_tiny",
            tolerance=1e-12,
            rng_algorithm="pcg64",
        ))
    except DiagnosticError as error:
        outcomes["tolerance_exceeded"] = error.to_dict()
    try:
        run_task(task, OperatorConfig(mask_priority=None, dtype="float32"))
    except DiagnosticError as error:
        outcomes["mask_priority_invalid"] = error.to_dict()
    return outcomes


def cmd_demo(args: argparse.Namespace) -> int:
    out_path = Path(args.out)
    suite_path = Path(args.suite)
    manifest = generate_suite(suite_path, seed=args.seed)
    configs = {
        "float64": OperatorConfig(
            mask_priority="mask_first",
            dtype="float64",
            rounding_mode="none",
            subnormal_guard="clamp_tiny",
            tolerance=1e-5,
        ),
        "float32": OperatorConfig(
            mask_priority="mask_first",
            dtype="float32",
            rounding_mode="none",
            subnormal_guard="clamp_tiny",
            tolerance=1e-5,
        ),
    }
    selfchecks = {
        dtype: run_self_check(suite_path, config=config) for dtype, config in configs.items()
    }
    for dtype, report in selfchecks.items():
        _write_json(out_path / f"selfcheck_{dtype}.json", report)
    migration = _migration_tour(suite_path)
    snapshots = _snapshot_tour(suite_path, configs["float32"])
    errors = _error_code_tour(suite_path, configs["float32"])
    demo_report = {
        "schema": "sampling-interop-demo-v1",
        "suite": {
            "path": str(suite_path.resolve()),
            "manifest": manifest,
        },
        "selfcheck": {
            dtype: {
                "summary": report["summary"],
                "report_checksum": report["report_checksum"],
                "cases": [
                    {
                        "case_id": case["case_id"],
                        "sample_sequences": case["sample_sequences"],
                        "error_spectrum_checksum": case["error_spectrum_checksum"],
                        "max_abs_error": case["max_abs_error"],
                    }
                    for case in report["cases"]
                ],
            }
            for dtype, report in selfchecks.items()
        },
        "migration": migration,
        "snapshots": snapshots,
        "error_codes": errors,
        "acceptance": {
            "three_format_normalization": selfchecks["float32"]["summary"]["all_consistent"],
            "topk_sampling_consistency": selfchecks["float64"]["summary"]["all_consistent"]
            and selfchecks["float32"]["summary"]["all_consistent"],
            "migration_semantics": all(
                all(targets.values()) for targets in migration["roundtrip_results"].values()
            )
            and migration["incremental_revision_to_v1"]["semantic_complete"] is False
            and migration["strict_policy_error"]["error_code"] == "E_SEMANTIC_INCOMPLETE",
            "snapshot_replacement": snapshots["history_versions"] == [1, 2]
            and snapshots["rollback_error"]["error_code"] == "E_SNAPSHOT_ROLLBACK"
            and snapshots["concurrent_busy_error"]["error_code"] == "E_STAGE_BUSY",
            "subnormal_guard_recorded": any(
                len(case["reports"]["v3"]["subnormal_events"]) > 0
                for case in selfchecks["float32"]["cases"]
                if case["case_id"] == "case_precision_subnormal"
            ),
        },
    }
    demo_report["report_checksum"] = sha256_json(demo_report)
    _write_json(out_path / "demo_report.json", demo_report)
    print(
        json.dumps(
            {
                "status": "ok",
                "demo_report": str((out_path / "demo_report.json").resolve()),
                "suite_logical_checksum": manifest["logical_checksum"],
                "selfcheck_float64": selfchecks["float64"]["summary"],
                "selfcheck_float32": selfchecks["float32"]["summary"],
                "acceptance": demo_report["acceptance"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="llm-sampling-diag")
    subparsers = parser.add_subparsers(dest="command", required=True)

    generate = subparsers.add_parser("generate", help="generate deterministic v1/v2/v3 samples")
    generate.add_argument("--out", default="samples")
    generate.add_argument("--seed", type=int, default=20261126)
    generate.set_defaults(handler=cmd_generate)

    selfcheck = subparsers.add_parser("selfcheck", help="normalize and re-run all formats")
    selfcheck.add_argument("--suite", default="samples")
    selfcheck.add_argument("--out", default="artifacts/selfcheck.json")
    _add_config_arguments(selfcheck)
    selfcheck.set_defaults(handler=cmd_selfcheck)

    demo = subparsers.add_parser("demo", help="run the complete acceptance example")
    demo.add_argument("--suite", default="samples")
    demo.add_argument("--out", default="artifacts/demo")
    demo.add_argument("--seed", type=int, default=20261126)
    demo.set_defaults(handler=cmd_demo)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except DiagnosticError as error:
        print(json.dumps(error.to_dict(), ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

