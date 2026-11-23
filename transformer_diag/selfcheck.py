from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .cache import ContentAddressedCache
from .compute import (
    compare_arrays,
    decode_array,
    execute_pipeline,
    output_array_from_payload,
    rebuild_statistics_from_blocks,
)
from .diagnostics import load_deviation_detail
from .errors import BusinessError
from .models import JobSpec, RuntimeConfig, STAGES
from .snapshots import SnapshotStore
from .util import (
    array_content_hash,
    atomic_write_json,
    npy_bytes,
    read_json,
    sanitize_component,
    sha256_bytes,
    sha256_value,
    utc_now,
)
from .workspace import Workspace


class CheckLog:
    def __init__(self) -> None:
        self.checks: list[dict[str, Any]] = []

    def add(self, name: str, passed: bool, details: Any = None) -> None:
        self.checks.append(
            {
                "name": name,
                "passed": bool(passed),
                "details": details if details is not None else {},
            }
        )

    @property
    def passed(self) -> bool:
        return all(item["passed"] for item in self.checks)


def run_selfcheck(root: str | Path | Workspace) -> dict[str, Any]:
    workspace = root if isinstance(root, Workspace) else Workspace(root)
    workspace.initialize()
    report_path = workspace.paths.latest_report
    if not report_path.exists():
        raise BusinessError(
            "E_INCONSISTENT",
            "no report is available for self-check",
            field=str(report_path),
            stage="selfcheck",
        )
    report = read_json(report_path, code="E_CACHE_CORRUPT")
    if not isinstance(report, dict):
        raise BusinessError(
            "E_CACHE_CORRUPT", "report is not a JSON object", field=str(report_path)
        )
    config = RuntimeConfig.from_dict(report.get("config"))
    cache = ContentAddressedCache(workspace)
    snapshots = SnapshotStore(workspace)
    log = CheckLog()

    invalidation = report.get("invalidation", {})
    recomputed = set(invalidation.get("recomputed_nodes", []))
    reused = set(invalidation.get("reused_nodes", []))
    affected = set(invalidation.get("affected_nodes", []))
    overlap = sorted(recomputed & reused)
    missing_affected = sorted(affected - (recomputed | reused))
    log.add(
        "recomputed_reused_disjoint",
        not overlap,
        {"overlap": overlap},
    )
    log.add(
        "affected_covered_by_recompute_or_reuse",
        not missing_affected,
        {"uncovered": missing_affected},
    )

    jobs = report.get("jobs", {})
    if not isinstance(jobs, dict):
        jobs = {}
    stage_sets: dict[str, set[str]] = {stage: set() for stage in STAGES}
    complete_jobs: set[str] = set()
    missing_stages: dict[str, list[str]] = {}
    for job_id, job_report in jobs.items():
        nodes = job_report.get("nodes", {})
        stages_present = {
            stage
            for stage in STAGES
            if nodes.get(stage, {}).get("status") in {"hit", "recomputed"}
        }
        for stage in stages_present:
            stage_sets[stage].add(job_id)
        if len(stages_present) == len(STAGES):
            complete_jobs.add(job_id)
        else:
            missing_stages[job_id] = sorted(set(STAGES) - stages_present)
    stage_sets_equal = all(stage_sets[stage] == complete_jobs for stage in STAGES)
    log.add(
        "four_stage_job_sets_equal",
        stage_sets_equal,
        {
            "stage_job_sets": {stage: sorted(values) for stage, values in stage_sets.items()},
            "complete_jobs": sorted(complete_jobs),
            "incomplete_jobs": missing_stages,
        },
    )
    reported_completed = set(report.get("completed_job_ids", []))
    log.add(
        "reported_completed_jobs_have_all_four_stages",
        reported_completed.issubset(complete_jobs),
        {
            "reported_completed_jobs": sorted(reported_completed),
            "complete_jobs": sorted(complete_jobs),
            "missing": sorted(reported_completed - complete_jobs),
        },
    )

    cache_ok = True
    cache_failures: list[dict[str, Any]] = []
    current_nodes: dict[str, dict[str, Any]] = {}
    for job_id, job_report in jobs.items():
        for stage in STAGES:
            node = job_report.get("nodes", {}).get(stage)
            if not node or node.get("status") not in {"hit", "recomputed"}:
                continue
            node_key = f"{job_id}/{stage}"
            try:
                cached = cache.get(
                    node["cache_key"],
                    expected_job_id=job_id,
                    expected_stage=stage,
                )
                if cached is None or cached["content_hash"] != node["content_hash"]:
                    raise BusinessError(
                        "E_CACHE_CORRUPT",
                        "node cache hash does not match cache object",
                        job_id=job_id,
                        stage=stage,
                        field="content_hash",
                    )
                current_nodes[node_key] = cached
            except BusinessError as exc:
                cache_ok = False
                cache_failures.append(exc.to_dict())
    log.add(
        "cache_objects_verify",
        cache_ok,
        {"failures": cache_failures},
    )

    stats_checks: list[dict[str, Any]] = []
    stats_all_ok = True
    for job_id in sorted(complete_jobs):
        job_report = jobs[job_id]
        job = JobSpec.from_dict(job_report["spec"])
        block_cached = current_nodes.get(f"{job_id}/block_stats")
        normalize_cached = current_nodes.get(f"{job_id}/normalize")
        if block_cached is None or normalize_cached is None:
            stats_all_ok = False
            stats_checks.append({"job_id": job_id, "passed": False, "reason": "cache_missing"})
            continue
        block_value = block_cached["payload"]["value"]
        normalize_value = normalize_cached["payload"]["value"]
        rebuilt = rebuild_statistics_from_blocks(job.op_kind, block_value)
        stored = {
            name: decode_array(value)
            for name, value in normalize_value["merged_stats"].items()
        }
        fields: dict[str, Any] = {}
        job_ok = True
        for name in rebuilt:
            comparison = compare_arrays(
                rebuilt[name], stored[name], config.tolerance, stat=True
            )
            fields[name] = comparison
            if comparison["exceed_count"]:
                job_ok = False
        stats_all_ok = stats_all_ok and job_ok
        stats_checks.append({"job_id": job_id, "passed": job_ok, "fields": fields})
    log.add(
        "block_stats_remerge_to_whole_statistics",
        stats_all_ok,
        {"jobs": stats_checks},
    )

    deviation_checks: list[dict[str, Any]] = []
    deviations_ok = True
    for job_id in sorted(complete_jobs):
        job_report = jobs[job_id]
        diagnostics = job_report.get("diagnostics", {})
        detail_info = diagnostics.get("deviation_detail", {})
        detail_path = detail_info.get("report_path") or detail_info.get("path")
        if not detail_path:
            deviations_ok = False
            deviation_checks.append({"job_id": job_id, "passed": False, "reason": "no_detail_path"})
            continue
        path = Path(detail_path)
        if not path.is_absolute():
            path = workspace.paths.root / path
        try:
            detail = load_deviation_detail(path)
        except BusinessError as exc:
            deviations_ok = False
            deviation_checks.append(
                {"job_id": job_id, "passed": False, "reason": exc.message}
            )
            continue
        block = detail["block"]
        whole = detail["whole"]
        recomputed_summary = compare_arrays(block, whole, config.tolerance)
        stored_summary = diagnostics.get("deviation", {})
        block_flat = np.asarray(block, dtype=np.float64).reshape(-1)
        whole_flat = np.asarray(whole, dtype=np.float64).reshape(-1)
        abs_flat = np.abs(block_flat - whole_flat)
        rel_flat = abs_flat / np.maximum(
            np.abs(whole_flat), np.finfo(np.float64).tiny
        )
        top_count = min(64, abs_flat.size)
        top_indices = (
            np.argsort(abs_flat, kind="stable")[-top_count:][::-1].tolist()
            if top_count
            else []
        )
        stored_spectrum = stored_summary.get("spectrum", [])
        spectrum_matches = len(stored_spectrum) == len(top_indices)
        if spectrum_matches:
            for stored_item, flat_index in zip(stored_spectrum, top_indices):
                if not isinstance(stored_item, dict):
                    spectrum_matches = False
                    break
                spectrum_matches = spectrum_matches and (
                    int(stored_item.get("flat_index")) == int(flat_index)
                    and np.isclose(
                        float(stored_item.get("abs")),
                        float(abs_flat[flat_index]),
                        rtol=1e-12,
                        atol=0.0,
                    )
                    and np.isclose(
                        float(stored_item.get("rel")),
                        float(rel_flat[flat_index]),
                        rtol=1e-12,
                        atol=0.0,
                    )
                )
        checks = {
            "shape": block.shape == whole.shape,
            "abs_detail": np.array_equal(
                detail["abs_dev"], np.abs(block - whole)
            ),
            "rel_detail": np.array_equal(
                detail["rel_dev"],
                np.abs(block - whole)
                / np.maximum(
                    np.abs(whole), np.finfo(np.float64).tiny
                ),
            ),
            "max_abs": np.isclose(
                recomputed_summary["max_abs"],
                stored_summary.get("max_abs", float("nan")),
                rtol=1e-12,
                atol=0.0,
            ),
            "max_rel": np.isclose(
                recomputed_summary["max_rel"],
                stored_summary.get("max_rel", float("nan")),
                rtol=1e-12,
                atol=0.0,
            ),
            "max_abs_index": recomputed_summary["max_abs_flat_index"]
            == stored_summary.get("max_abs_flat_index"),
            "max_rel_index": recomputed_summary["max_rel_flat_index"]
            == stored_summary.get("max_rel_flat_index"),
            "exceed_count": recomputed_summary["exceed_count"]
            == stored_summary.get("exceed_count"),
            "element_count": recomputed_summary["element_count"]
            == stored_summary.get("element_count"),
            "spectrum": spectrum_matches,
            "detail_sha256": sha256_bytes(path.read_bytes()) == detail_info.get("sha256"),
        }
        job_ok = all(bool(value) for value in checks.values())
        deviations_ok = deviations_ok and job_ok
        deviation_checks.append(
            {"job_id": job_id, "passed": job_ok, "checks": checks}
        )
    log.add(
        "deviation_spectrum_recomputed_from_detail",
        deviations_ok,
        {"jobs": deviation_checks},
    )

    determinism_checks: list[dict[str, Any]] = []
    determinism_ok = True
    output_checks_ok = True
    for job_id in sorted(complete_jobs):
        job_report = jobs[job_id]
        job = JobSpec.from_dict(job_report["spec"])
        input_path = workspace.paths.inputs / f"{sanitize_component(job_id)}.npy"
        try:
            array = np.load(input_path, allow_pickle=False)
        except (OSError, ValueError) as exc:
            determinism_ok = False
            output_checks_ok = False
            determinism_checks.append(
                {"job_id": job_id, "passed": False, "reason": f"input load failed: {exc}"}
            )
            continue
        input_matches = array_content_hash(array) == job_report.get("input_hash")
        pipeline_a = execute_pipeline(job, array, config.policy)
        pipeline_b = execute_pipeline(job, array, config.policy)
        hashes_a = {stage: sha256_value(value) for stage, value in pipeline_a.items()}
        hashes_b = {stage: sha256_value(value) for stage, value in pipeline_b.items()}
        deterministic = hashes_a == hashes_b
        output_array = output_array_from_payload(pipeline_a["output"])
        output_sha = sha256_bytes(npy_bytes(output_array))
        report_sha = job_report.get("output_sha256")
        output_matches = output_sha == report_sha
        output_path = workspace.paths.outputs / f"{sanitize_component(job_id)}.npy"
        file_matches = output_path.exists() and sha256_bytes(output_path.read_bytes()) == report_sha
        job_ok = deterministic and input_matches and output_matches and file_matches
        determinism_ok = determinism_ok and job_ok
        output_checks_ok = output_checks_ok and output_matches and file_matches
        determinism_checks.append(
            {
                "job_id": job_id,
                "passed": job_ok,
                "deterministic_stage_hashes": deterministic,
                "stage_hashes": hashes_a,
                "input_hash_matches_report": input_matches,
                "output_matches_report": output_matches,
                "output_file_matches_report": file_matches,
            }
        )
    log.add(
        "repeat_execution_deterministic",
        determinism_ok,
        {"jobs": determinism_checks},
    )
    log.add(
        "output_files_match_artifacts",
        output_checks_ok,
        {"jobs": determinism_checks},
    )

    snapshot_failures: list[dict[str, Any]] = []
    snapshot_ok = True
    for expected in report.get("snapshot_inventory", []):
        try:
            actual = snapshots.read(expected["kind"], expected["key"])
            if actual is None or actual["version"] != expected["version"]:
                snapshot_ok = False
                snapshot_failures.append(
                    {"kind": expected["kind"], "key": expected["key"], "reason": "missing_or_version"}
                )
        except BusinessError as exc:
            snapshot_ok = False
            snapshot_failures.append(exc.to_dict())
    log.add(
        "snapshots_verify",
        snapshot_ok,
        {"failures": snapshot_failures},
    )

    consistency = report.get("consistency", {})
    consistency_jobs = consistency.get("jobs", {})
    consistency_ok = bool(consistency.get("all_byte_identical")) and all(
        bool(item.get("byte_identical")) for item in consistency_jobs.values()
    )
    log.add(
        "incremental_full_byte_consistency",
        consistency_ok,
        {
            "all_byte_identical": consistency.get("all_byte_identical"),
            "jobs": {
                job_id: item.get("byte_identical")
                for job_id, item in consistency_jobs.items()
            },
        },
    )

    unattributed = [
        error for error in report.get("errors", []) if error.get("code") == "E_UNATTRIBUTED"
    ]
    unattributed_attributed = all(
        bool(error.get("stage") and error.get("message") and error.get("details"))
        for error in unattributed
    )
    log.add(
        "unattributed_exceptions_are_recorded",
        unattributed_attributed,
        {"count": len(unattributed)},
    )

    passed = log.passed
    result = {
        "schema": 1,
        "ok": passed,
        "code": "OK" if passed else "E_INCONSISTENT",
        "generated_at": utc_now(),
        "report_path": str(report_path),
        "run_id": report.get("run_id"),
        "checks": log.checks,
        "failed_checks": [item["name"] for item in log.checks if not item["passed"]],
        "summary": {
            "complete_jobs": sorted(complete_jobs),
            "stage_job_sets": {stage: sorted(values) for stage, values in stage_sets.items()},
            "recomputed_nodes": sorted(recomputed),
            "reused_nodes": sorted(reused),
            "affected_nodes": sorted(affected),
        },
    }
    atomic_write_json(workspace.paths.latest_selfcheck, result, pretty=True)
    return result

