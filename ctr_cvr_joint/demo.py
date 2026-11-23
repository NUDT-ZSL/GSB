"""Command-line demonstration over the committed deterministic fixtures."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
from pathlib import Path
from typing import Any, Callable

from .cache import StageLock
from .engine import EngineConfig, JointCTREngine
from .errors import (
    BusinessError,
    E_CACHE_CORRUPT,
    E_DUP_REQUEST,
    E_FORMAT_INVALID,
    E_LINEAGE_BROKEN,
    E_RESOURCE_LIMIT,
    E_SNAPSHOT_ROLLBACK,
    E_STAGE_BUSY,
    E_THRESHOLD_INVALID,
    E_TOLERANCE_INVALID,
    E_VERSION_UNSUPPORTED,
)
from .hashing import (
    atomic_write_json,
    read_json,
    sha256_bytes,
    sha256_obj,
)
from .migration import migrate_spec
from .models import ThresholdConfig
from .samples import (
    apply_label_updates,
    load_samples_jsonl,
    load_samples_parquet,
    order_samples,
    samples_checksum,
)
from .specs import load_spec, load_spec_bytes
from .statistics import load_statistics


def _capture(label: str, function: Callable[[], Any]) -> dict[str, Any]:
    try:
        value = function()
        return {
            "label": label,
            "ok": True,
            "value": value,
        }
    except BusinessError as exc:
        return {
            "label": label,
            "ok": False,
            "code": exc.code,
            "message": exc.message,
            "line": exc.line,
            "field": exc.field,
            "details": exc.details,
        }
    except Exception as exc:  # pragma: no cover - demo diagnostic
        return {
            "label": label,
            "ok": False,
            "code": "E_UNEXPECTED",
            "message": str(exc),
            "details": {"type": type(exc).__name__},
        }


def _expect_code(result: dict[str, Any], expected: str, checks: dict[str, Any]) -> None:
    checks[result["label"]] = {
        "expected_code": expected,
        "actual_code": result.get("code"),
        "passed": (not result["ok"]) and result.get("code") == expected,
    }


def _run_summary(report: Any) -> dict[str, Any]:
    data = report.data
    return {
        "run_label": data["run_label"],
        "samples": data["samples"],
        "feature_spec": data["feature_spec"],
        "statistics": data["statistics"],
        "threshold": data["threshold"],
        "assembly": {
            "shape": data["assembly_summary"]["shape"],
            "matrix_sha256": data["assembly_summary"]["matrix_sha256"],
            "summary_sha256": data["assembly_summary"]["summary_sha256"],
        },
        "scoring": {
            "score_hash": data["score_summary"]["score_hash"],
            "probability_hash": data["score_summary"]["probability_hash"],
        },
        "accumulation": {
            "checksum": data["accumulation_summary"]["checksum"],
            "row_count": data["accumulation_summary"]["payload"]["row_count"],
        },
        "cache_nodes": {
            node: {
                "affected_count": len(node_info["affected_samples"]),
                "recomputed_count": node_info["recomputed_count"],
                "reused_count": node_info["reused_count"],
                "recomputed_samples": node_info["recomputed_samples"],
                "reused_samples": node_info["reused_samples"],
            }
            for node, node_info in data["cache"]["nodes"].items()
        },
        "invalidation": {
            key: data["invalidation"][key]
            for key in (
                "change_kind",
                "detected_changes",
                "affected_nodes",
                "recomputed_nodes",
                "affected_request_ids",
                "affected_request_ids_by_node",
                "recomputed_request_ids",
                "reused_request_ids",
                "added_request_ids",
                "removed_request_ids",
                "label_corrected_request_ids",
                "changed_spec_paths",
                "changed_statistics_paths",
                "affected_features",
                "affected_derived_features",
            )
        },
        "degraded": data["degraded"],
        "self_check": data["self_check"],
        "score_result_snapshot": data["score_result_snapshot"],
    }


def _core_compare(left: Any, right: Any) -> dict[str, Any]:
    checks = {
        "matrix_sha256_equal": left.data["assembly_summary"]["matrix_sha256"]
        == right.data["assembly_summary"]["matrix_sha256"],
        "score_hash_equal": left.data["score_summary"]["score_hash"]
        == right.data["score_summary"]["score_hash"],
        "accumulation_checksum_equal": left.data["accumulation_summary"]["checksum"]
        == right.data["accumulation_summary"]["checksum"],
    }
    checks["passed"] = all(checks.values())
    checks["values"] = {
        side: {
            "matrix_sha256": report.data["assembly_summary"]["matrix_sha256"],
            "score_hash": report.data["score_summary"]["score_hash"],
            "accumulation_checksum": report.data["accumulation_summary"]["checksum"],
        }
        for side, report in (("incremental", left), ("full", right))
    }
    return checks


def _write_auxiliary_reports(root: Path, report: dict[str, Any]) -> dict[str, str]:
    files = {
        "migrations": root / "migrations.json",
        "assembly_summary": root / "assembly_summary.json",
        "contributions": root / "contributions.json",
        "invalidation": root / "invalidation.json",
        "business_codes": root / "business_codes.json",
        "runs": root / "run_summaries.json",
    }
    atomic_write_json(files["migrations"], report["migrations"])
    atomic_write_json(files["assembly_summary"], report["initial_run"]["assembly_summary"])
    atomic_write_json(files["contributions"], report["initial_run"]["contributions"])
    atomic_write_json(files["invalidation"], report["runs"])
    atomic_write_json(files["business_codes"], report["business_codes"])
    atomic_write_json(files["runs"], report["runs"])
    return {name: str(path) for name, path in files.items()}


def run_demo(data_dir: str | Path, out_dir: str | Path) -> dict[str, Any]:
    data = Path(data_dir).resolve()
    out = Path(out_dir).resolve()
    if out == data or out in data.parents:
        raise ValueError("output directory cannot contain the fixture directory")
    if out.exists():
        for child in out.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    out.mkdir(parents=True, exist_ok=True)
    workspace = out / "workspace"

    manifest = read_json(data / "MANIFEST.json")
    fixture_checks: dict[str, bool] = {}
    for name, expected in manifest["files"].items():
        fixture_checks[name] = sha256_bytes((data / name).read_bytes()) == expected
    if not all(fixture_checks.values()):
        raise RuntimeError("committed fixture checksum mismatch")

    samples = load_samples_jsonl(data / "requests.jsonl")
    parquet_samples = load_samples_parquet(data / "requests.parquet")
    parquet_consistent = [sample.to_dict() for sample in samples] == [
        sample.to_dict() for sample in parquet_samples
    ]
    spec_v1 = load_spec(data / "spec_v1.txt")
    spec_v2 = load_spec(data / "spec_v2.json")
    spec_v3 = load_spec(data / "spec_v3.json")
    spec_v3_rev2 = load_spec(data / "spec_v3_rev2.json")
    stats_v1 = load_statistics(data / "stats_v1.json")
    stats_v2 = load_statistics(data / "stats_v2.json")
    readers = {
        "specs": {
            "v1": spec_v1.to_dict(),
            "v2": spec_v2.to_dict(),
            "v3": spec_v3.to_dict(),
            "v3_revision2": spec_v3_rev2.to_dict(),
        },
        "statistics": {
            "v1": stats_v1.to_dict(),
            "v2": stats_v2.to_dict(),
        },
        "samples": {
            "jsonl_count": len(samples),
            "parquet_count": len(parquet_samples),
            "jsonl_checksum": samples_checksum(samples),
            "parquet_matches_jsonl": parquet_consistent,
            "fields": manifest["sample_fields"],
        },
        "fixture_checksums_valid": fixture_checks,
    }

    migration_results = {
        "v1_to_v2": migrate_spec(spec_v1, 2),
        "v1_to_v3": migrate_spec(spec_v1, 3),
        "v2_to_v3": migrate_spec(spec_v2, 3),
        "v3_to_v2": migrate_spec(spec_v3, 2),
        "v3_to_v1": migrate_spec(spec_v3, 1),
        "v2_to_v1": migrate_spec(spec_v2, 1),
    }
    migrations_report = {
        name: {
            **result.to_dict(),
            "target_text": result.target_text,
            "all_records_replayed": result.replay_ok,
        }
        for name, result in migration_results.items()
    }
    for name, result in migration_results.items():
        extension = ".txt" if result.target_version == 1 else ".json"
        (out / f"migrated_{name}{extension}").write_text(
            result.target_text, encoding="utf-8"
        )

    strict = EngineConfig(
        seed=manifest["seed"],
        threshold=ThresholdConfig(
            policy="strict",
            click_threshold=0.45,
            cvr_threshold=0.40,
            tolerance=0.02,
            stable_digits=12,
        ),
        joint_click_scale=0.25,
        max_feature_matrix_bytes=64 * 1024 * 1024,
        max_parallel_nodes=4,
        parallel_nodes=1,
    )
    tolerance = EngineConfig(
        seed=manifest["seed"],
        threshold=ThresholdConfig(
            policy="tolerance",
            click_threshold=0.45,
            cvr_threshold=0.40,
            tolerance=0.02,
            stable_digits=12,
        ),
        joint_click_scale=0.25,
        max_feature_matrix_bytes=64 * 1024 * 1024,
        max_parallel_nodes=4,
        parallel_nodes=1,
    )
    engine = JointCTREngine(workspace, strict)
    initial = engine.run_full(
        samples, spec_v3, stats_v1, spec_snapshot_version=3, run_label="initial_full"
    )
    initial_trace = initial.data["traces"][0]
    rerun = engine.run_full(
        samples, spec_v3, stats_v1, spec_snapshot_version=3, run_label="same_input_rerun"
    )
    cache_probe = engine.explain_request("req-0001")
    current_replay = engine.replay_latest("req-0001")

    engine.config = tolerance
    threshold_run = engine.run_incremental(
        samples,
        spec_v3,
        stats_v1,
        spec_snapshot_version=3,
        change_kind="threshold",
        run_label="threshold_only",
    )
    stale_threshold = _capture(
        "stale_trace_after_threshold_change",
        lambda: engine.replay_trace(initial_trace),
    )
    full_threshold = JointCTREngine(out / "full_threshold_workspace", tolerance).run_full(
        samples,
        spec_v3,
        stats_v1,
        spec_snapshot_version=3,
        run_label="full_threshold_reference",
    )

    stats_run = engine.run_incremental(
        samples,
        spec_v3,
        stats_v2,
        change_kind="stats",
        run_label="statistics_update",
    )
    full_stats = JointCTREngine(out / "full_stats_workspace", tolerance).run_full(
        samples,
        spec_v3,
        stats_v2,
        spec_snapshot_version=3,
        run_label="full_stats_reference",
    )

    label_updates = {
        "req-0002": {"label_click": 0, "label_convert": 0},
        "req-0005": {"label_click": 0, "label_convert": 0},
    }
    corrected_samples = apply_label_updates(samples, label_updates)
    labels_run = engine.run_incremental(
        corrected_samples,
        spec_v3,
        stats_v2,
        change_kind="labels",
        run_label="label_correction",
    )
    pre_spec_trace = labels_run.data["traces"][0]
    full_labels = JointCTREngine(out / "full_labels_workspace", tolerance).run_full(
        corrected_samples,
        spec_v3,
        stats_v2,
        spec_snapshot_version=3,
        run_label="full_labels_reference",
    )

    spec_run = engine.run_incremental(
        corrected_samples,
        spec_v3_rev2,
        stats_v2,
        spec_snapshot_version=4,
        change_kind="spec",
        run_label="feature_spec_revision",
    )
    full_spec = JointCTREngine(out / "full_spec_workspace", tolerance).run_full(
        corrected_samples,
        spec_v3_rev2,
        stats_v2,
        spec_snapshot_version=4,
        run_label="full_spec_reference",
    )
    stale_spec = _capture(
        "stale_trace_after_feature_spec_change",
        lambda: engine.replay_trace(pre_spec_trace),
    )
    final_replay = engine.replay_latest("req-0001")
    final_probe = engine.explain_request("req-0001")

    comparisons = {
        "threshold_only_incremental_vs_full": _core_compare(threshold_run, full_threshold),
        "statistics_update_incremental_vs_full": _core_compare(stats_run, full_stats),
        "label_correction_incremental_vs_full": _core_compare(labels_run, full_labels),
        "feature_spec_incremental_vs_full": _core_compare(spec_run, full_spec),
    }

    rollback = _capture(
        "snapshot_rollback",
        lambda: engine.run_full(
            corrected_samples,
            spec_v3,
            stats_v2,
            spec_snapshot_version=3,
            run_label="rollback_attempt",
        ),
    )

    def corrupt_cache() -> dict[str, Any]:
        corrupt_engine = JointCTREngine(out / "corrupt_workspace", strict)
        report = corrupt_engine.run_full(
            samples,
            spec_v3,
            stats_v1,
            spec_snapshot_version=3,
            run_label="corrupt_baseline",
        )
        key = report.data["lineage_index"]["req-0001"]["score_cache_key"]
        path = corrupt_engine.cache._path("scoring", key)
        envelope = read_json(path)
        envelope["product"]["row"]["click"]["probability"] = 0.123456789
        path.write_text(json.dumps(envelope), encoding="utf-8")
        from .cache import CacheKey

        corrupt_engine.cache.get("scoring", CacheKey(key, envelope["components"]))
        return {"unexpected": True}

    cache_corrupt = _capture("cache_checksum_corrupt", corrupt_cache)

    def snapshot_corrupt() -> dict[str, Any]:
        corrupt_engine = JointCTREngine(out / "snapshot_corrupt_workspace", strict)
        corrupt_engine.run_full(
            samples,
            spec_v3,
            stats_v1,
            spec_snapshot_version=3,
            run_label="snapshot_corrupt_baseline",
        )
        info = corrupt_engine.snapshots.current_info("feature_spec")
        assert info is not None
        path = Path(info.path)
        wrapper = read_json(path)
        wrapper["payload"]["spec"]["source_version"] = 999
        path.write_text(json.dumps(wrapper), encoding="utf-8")
        corrupt_engine.snapshots.load("feature_spec", info.version)
        return {"unexpected": True}

    snapshot_corrupt = _capture("snapshot_checksum_corrupt", snapshot_corrupt)

    def stage_busy() -> dict[str, Any]:
        with StageLock(engine.workspace):
            engine.run_incremental(
                corrected_samples,
                spec_v3_rev2,
                stats_v2,
                spec_snapshot_version=4,
                run_label="busy_attempt",
            )
        return {"unexpected": True}

    stage_busy_result = _capture("concurrent_stage_busy", stage_busy)

    resource_limit = _capture(
        "feature_matrix_resource_limit",
        lambda: JointCTREngine(
            out / "resource_workspace",
            EngineConfig(
                seed=manifest["seed"],
                threshold=strict.threshold,
                max_feature_matrix_bytes=8,
            ),
        ).run_full(
            samples,
            spec_v3,
            stats_v1,
            spec_snapshot_version=3,
            run_label="resource_limit_attempt",
        ),
    )

    degraded_engine = JointCTREngine(
        out / "degraded_workspace",
        EngineConfig(
            seed=manifest["seed"],
            threshold=strict.threshold,
            max_parallel_nodes=2,
            parallel_nodes=8,
        ),
    )
    degraded_run = degraded_engine.run_full(
        samples,
        spec_v3,
        stats_v1,
        spec_snapshot_version=3,
        run_label="degraded_parallelism",
    )

    threshold_invalid = _capture(
        "invalid_threshold",
        lambda: ThresholdConfig(policy="strict", click_threshold=1.5),
    )
    tolerance_invalid = _capture(
        "invalid_tolerance",
        lambda: ThresholdConfig(policy="tolerance", tolerance=-0.1),
    )
    duplicate_request = _capture(
        "duplicate_request",
        lambda: order_samples(samples + [samples[0]]),
    )
    version_unsupported = _capture(
        "unsupported_spec_version",
        lambda: load_spec_bytes(b'{"format_version": 99}'),
    )
    format_invalid = _capture(
        "format_invalid_line_and_field",
        lambda: load_spec_bytes(b"version=1\nN|ctr_base|not-a-number|0.1|0.5\n"),
    )

    expected_code_results = {
        "stale_threshold_trace": stale_threshold,
        "stale_spec_trace": stale_spec,
        "snapshot_rollback": rollback,
        "cache_checksum_corrupt": cache_corrupt,
        "snapshot_checksum_corrupt": snapshot_corrupt,
        "concurrent_stage_busy": stage_busy_result,
        "feature_matrix_resource_limit": resource_limit,
        "invalid_threshold": threshold_invalid,
        "invalid_tolerance": tolerance_invalid,
        "duplicate_request": duplicate_request,
        "unsupported_spec_version": version_unsupported,
        "format_invalid": format_invalid,
    }
    expected_codes = {
        "stale_threshold_trace": E_LINEAGE_BROKEN,
        "stale_spec_trace": E_LINEAGE_BROKEN,
        "snapshot_rollback": E_SNAPSHOT_ROLLBACK,
        "cache_checksum_corrupt": E_CACHE_CORRUPT,
        "snapshot_checksum_corrupt": E_CACHE_CORRUPT,
        "concurrent_stage_busy": E_STAGE_BUSY,
        "feature_matrix_resource_limit": E_RESOURCE_LIMIT,
        "invalid_threshold": E_THRESHOLD_INVALID,
        "invalid_tolerance": E_TOLERANCE_INVALID,
        "duplicate_request": E_DUP_REQUEST,
        "unsupported_spec_version": E_VERSION_UNSUPPORTED,
        "format_invalid": E_FORMAT_INVALID,
    }
    code_checks: dict[str, Any] = {}
    for label, expected in expected_codes.items():
        _expect_code(expected_code_results[label], expected, code_checks)

    # Persist compact and detailed node artifacts from the initial run.
    initial_matrix_path = out / "initial_assembly.matrix"
    initial_matrix_path.write_bytes(initial.matrix_bytes or b"")
    atomic_write_json(out / "initial_assembly.summary.json", initial.data["assembly_summary"])
    atomic_write_json(
        out / "initial_contributions.json", initial.data["contributions"]
    )

    runs = {
        "initial_full": _run_summary(initial),
        "same_input_rerun": _run_summary(rerun),
        "threshold_only": _run_summary(threshold_run),
        "statistics_update": _run_summary(stats_run),
        "label_correction": _run_summary(labels_run),
        "feature_spec_revision": _run_summary(spec_run),
        "degraded_parallelism": _run_summary(degraded_run),
    }
    checks = {
        "fixture_checksums_valid": all(fixture_checks.values()),
        "jsonl_parquet_consistent": parquet_consistent,
        "all_migrations_replay": all(
            result.replay_ok for result in migration_results.values()
        ),
        "same_input_all_nodes_reused": all(
            runs["same_input_rerun"]["cache_nodes"][node]["recomputed_count"] == 0
            for node in ("assembly", "scoring", "accumulation")
        ),
        "threshold_only_scoring_recomputed": (
            runs["threshold_only"]["cache_nodes"]["assembly"]["recomputed_count"] == 0
            and runs["threshold_only"]["cache_nodes"]["scoring"]["recomputed_count"]
            == len(samples)
            and runs["threshold_only"]["cache_nodes"]["accumulation"][
                "recomputed_count"
            ]
            == 0
        ),
        "statistics_update_targeted": (
            runs["statistics_update"]["cache_nodes"]["assembly"]["recomputed_count"]
            == 0
            and 0
            < runs["statistics_update"]["cache_nodes"]["scoring"]["recomputed_count"]
            < len(samples)
        ),
        "label_correction_reuses_scores": (
            runs["label_correction"]["cache_nodes"]["assembly"]["recomputed_count"]
            == 0
            and runs["label_correction"]["cache_nodes"]["scoring"]["recomputed_count"]
            == 0
            and runs["label_correction"]["cache_nodes"]["accumulation"][
                "recomputed_count"
            ]
            == len(label_updates)
        ),
        "feature_spec_upgrade_all_downstream": (
            runs["feature_spec_revision"]["cache_nodes"]["assembly"]["recomputed_count"]
            == len(samples)
            and runs["feature_spec_revision"]["cache_nodes"]["scoring"][
                "recomputed_count"
            ]
            == len(samples)
            and runs["feature_spec_revision"]["cache_nodes"]["accumulation"][
                "recomputed_count"
            ]
            == len(samples)
        ),
        "incremental_equals_full": all(
            comparison["passed"] for comparison in comparisons.values()
        ),
        "current_trace_replays": current_replay["matches"]
        and final_replay["matches"],
        "expected_business_codes": all(
            check["passed"] for check in code_checks.values()
        ),
        "degraded_parallelism_marked": degraded_run.data["degraded"]
        == ["parallelism_capped"],
        "self_checks_pass": all(
            run["self_check"]["ok"] for run in runs.values()
        ),
    }
    report: dict[str, Any] = {
        "schema_version": 1,
        "component": "ctr_cvr_joint",
        "environment": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "numpy": _version("numpy"),
            "pyarrow": _version("pyarrow"),
        },
        "business_code": "OK" if all(checks.values()) else "E_DEMO_ASSERTION",
        "checks": checks,
        "fixtures": {
            "manifest": manifest,
            "checksums_valid": fixture_checks,
        },
        "readers": readers,
        "migrations": migrations_report,
        "initial_run": {
            "summary": runs["initial_full"],
            "assembly_summary": initial.data["assembly_summary"],
            "contributions": initial.data["contributions"],
            "traces": initial.data["traces"],
        },
        "runs": runs,
        "incremental_equivalence": comparisons,
        "cache_probe": {
            "initial": cache_probe,
            "final": final_probe,
        },
        "trace_replay": {
            "current_initial": current_replay,
            "current_final": final_replay,
            "stale_after_threshold": stale_threshold,
            "stale_after_spec_change": stale_spec,
        },
        "business_codes": list(expected_code_results.values()),
        "business_code_checks": code_checks,
    }
    report["report_checksum"] = sha256_obj(
        {key: value for key, value in report.items() if key != "report_checksum"}
    )
    files = _write_auxiliary_reports(out, report)
    files["demo_report"] = str(out / "demo_report.json")
    atomic_write_json(out / "demo_report.json", report)
    report["files"] = files
    # Recompute so the checksum covers the file index as well.
    report["report_checksum"] = sha256_obj(
        {key: value for key, value in report.items() if key != "report_checksum"}
    )
    atomic_write_json(out / "demo_report.json", report)
    return report


def _version(module_name: str) -> str:
    try:
        module = __import__(module_name)
    except ImportError:
        return "missing"
    return str(getattr(module, "__version__", "unknown"))


def verify_demo(out_dir: str | Path) -> dict[str, Any]:
    report = read_json(Path(out_dir) / "demo_report.json")
    if not report.get("checks", {}).get("expected_business_codes"):
        raise RuntimeError("demo business-code checks failed")
    expected = report["report_checksum"]
    actual = sha256_obj(
        {key: value for key, value in report.items() if key != "report_checksum"}
    )
    return {
        "ok": expected == actual and report.get("business_code") == "OK",
        "expected_checksum": expected,
        "actual_checksum": actual,
        "business_code": report.get("business_code"),
        "checks": report.get("checks"),
    }


def generate_demo_data(data_dir: str | Path) -> dict[str, Any]:
    import subprocess

    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "generate_demo_data.py"
    subprocess.run(
        [sys.executable, str(script)],
        cwd=str(root),
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, "CTR_CVR_JOINT_DATA_DIR": str(Path(data_dir).resolve())},
    )
    return read_json(Path(data_dir) / "MANIFEST.json")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctr-cvr-joint",
        description="Deterministic local CTR/CVR joint estimation demo",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    demo = subparsers.add_parser("demo", help="run the full local demonstration")
    demo.add_argument("--data", default="data", help="fixture directory")
    demo.add_argument("--out", default="artifacts/demo", help="output directory")
    demo.add_argument(
        "--summary",
        action="store_true",
        help="print a compact JSON summary instead of the complete report",
    )
    verify = subparsers.add_parser("verify", help="verify a generated demo report")
    verify.add_argument("--out", default="artifacts/demo", help="demo output directory")
    generate = subparsers.add_parser("generate", help="regenerate deterministic fixtures")
    generate.add_argument("--data", default="data", help="fixture directory")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "demo":
            report = run_demo(args.data, args.out)
            if args.summary:
                payload = {
                    "business_code": report["business_code"],
                    "checks": report["checks"],
                    "runs": report["runs"],
                    "migrations": {
                        name: {
                            key: value
                            for key, value in migration.items()
                            if key != "target_text"
                        }
                        for name, migration in report["migrations"].items()
                    },
                    "business_codes": [
                        {
                            "label": item["label"],
                            "code": item.get("code"),
                            "message": item.get("message"),
                        }
                        for item in report["business_codes"]
                    ],
                    "report_checksum": report["report_checksum"],
                    "report_path": report["files"]["demo_report"],
                }
            else:
                payload = report
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if report["business_code"] == "OK" else 1
        if args.command == "verify":
            payload = verify_demo(args.out)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0 if payload["ok"] else 1
        if args.command == "generate":
            payload = generate_demo_data(args.data)
            print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
            return 0
    except Exception as exc:
        payload = {
            "business_code": "E_UNEXPECTED",
            "message": str(exc),
            "type": type(exc).__name__,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 2
    parser.error("unknown command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

