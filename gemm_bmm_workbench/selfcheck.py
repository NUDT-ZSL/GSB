from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np

from .cache import verify_key_composition
from .canonical import array_digest, read_json
from .errors import Codes, WorkbenchError
from .ledger import LedgerManager
from .runtime import Workbench
from .snapshots import STAGES, verify_snapshot_checksum


def run_selfcheck(state_dir: str | Path) -> dict[str, Any]:
    root = Path(state_dir)
    bench = Workbench(root)
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, details: dict[str, Any] | None = None) -> bool:
        checks.append({"name": name, "ok": bool(ok), "details": details or {}})
        return bool(ok)

    state = bench.load_committed_state()
    jobs = state.get("jobs", {})
    expected_jobs = sorted(jobs)

    # Snapshot checks and stage job-set consistency.
    run_id = state.get("run_id")
    snapshots = bench.snapshots.stage_snapshots_for_run(run_id) if run_id else []
    expected_stage_names = [name for name, _ in STAGES]
    actual_stage_names = [snapshot.get("stage") for snapshot in snapshots]
    record(
        "four_stage_snapshots_present",
        actual_stage_names == expected_stage_names,
        {"expected": expected_stage_names, "actual": actual_stage_names},
    )
    checksum_ok = all(verify_snapshot_checksum(snapshot) for snapshot in snapshots)
    record("snapshot_checksums_valid", checksum_ok, {"snapshot_count": len(snapshots)})

    stage_sets = {
        snapshot["stage"]: sorted(snapshot.get("completed_jobs", []))
        for snapshot in snapshots
        if snapshot.get("stage") in expected_stage_names
    }
    consistent = bool(stage_sets) and all(job_set == expected_jobs for job_set in stage_sets.values())
    record(
        "stage_job_sets_consistent",
        consistent,
        {"expected_jobs": expected_jobs, "stage_sets": stage_sets},
    )

    # Cache key composition and product hashes.
    key_failures: list[dict[str, Any]] = []
    verified_jobs: list[str] = []
    for job_id in expected_jobs:
        try:
            query = bench.query_cache(job_id)
            for node_name, node in query["nodes"].items():
                key_report = node["key_report"]
                if not verify_key_composition(key_report):
                    key_failures.append({"job_id": job_id, "node": node_name, "reason": "key_composition_mismatch"})
                if not node["object_exists"]:
                    key_failures.append({"job_id": job_id, "node": node_name, "reason": "cache_object_missing"})

            job_state = jobs[job_id]
            plan_node = job_state["nodes"]["validation_plan"]
            plan_payload = bench.cache.get_object(plan_node["key"])["payload"]
            if plan_payload.get("plan_hash") != plan_node["payload_hash"]:
                key_failures.append({"job_id": job_id, "node": "validation_plan", "reason": "payload_hash_mismatch"})

            accum_node = job_state["nodes"]["block_accumulate"]
            accum_payload = bench.cache.get_object(accum_node["key"])["payload"]
            if accum_payload.get("accumulation_hash") != accum_node["payload_hash"]:
                key_failures.append({"job_id": job_id, "node": "block_accumulate", "reason": "payload_hash_mismatch"})
            blob = bench.cache.load_blob(accum_payload["output_blob"])
            if array_digest(blob)["sha256"] != accum_payload["accumulation_hash"]:
                key_failures.append({"job_id": job_id, "node": "block_accumulate", "reason": "blob_hash_mismatch"})

            aggregate_node = job_state["nodes"]["aggregate_validate"]
            aggregate_payload = bench.cache.get_object(aggregate_node["key"])["payload"]
            if aggregate_payload.get("result_hash") != aggregate_node["payload_hash"]:
                key_failures.append({"job_id": job_id, "node": "aggregate_validate", "reason": "payload_hash_mismatch"})

            result_document = read_json(root / job_state["result_file"])
            result_output = np.asarray(result_document["output"], dtype=np.float64)
            if job_state["spec"]["op_kind"] == "gemm":
                result_output = np.expand_dims(result_output, axis=0)
            if array_digest(result_output)["sha256"] != job_state["result_hash"]:
                key_failures.append({"job_id": job_id, "node": "result_file", "reason": "output_hash_mismatch"})
            verified_jobs.append(job_id)
        except Exception as exc:  # noqa: BLE001 - selfcheck should report every verification failure.
            key_failures.append({"job_id": job_id, "reason": exc.__class__.__name__, "message": str(exc)})

    record(
        "cache_keys_and_product_hashes_verifiable",
        not key_failures,
        {"verified_jobs": verified_jobs, "failures": key_failures},
    )

    # Incremental recompute/reuse coverage.
    impact_path = root / "incremental-report.json"
    if impact_path.exists():
        impact = read_json(impact_path)
        recomputed = set(impact.get("recomputed_nodes", []))
        reused = set(impact.get("reused_nodes", []))
        active_jobs = impact.get("coverage", {}).get("affected_jobs", [])
        # Coverage universe is all active graph nodes, including unaffected jobs.
        all_active_jobs = {item["job_id"] for item in impact.get("jobs", [])}
        expected_nodes = {f"{job_id}:{node}" for job_id in all_active_jobs for node in ("validation_plan", "block_accumulate", "aggregate_validate")}
        coverage_ok = (
            not (recomputed & reused)
            and (recomputed | reused) == expected_nodes
            and impact.get("coverage", {}).get("union_covers_all_graph_nodes") is True
        )
        record(
            "incremental_sets_partition_graph",
            coverage_ok,
            {
                "intersection": sorted(recomputed & reused),
                "missing_from_union": sorted(expected_nodes - (recomputed | reused)),
                "unexpected_in_union": sorted((recomputed | reused) - expected_nodes),
                "affected_jobs": active_jobs,
            },
        )
    else:
        record("incremental_sets_partition_graph", False, {"reason": "incremental-report.json missing"})

    # Ledger semantic consistency.
    pairs = bench.ledger.semantic_pairs()
    ledger_ok = bool(pairs) and all(pair["semantic_equal"] for pair in pairs) and len(pairs) == len(expected_jobs)
    record(
        "dual_ledger_semantic_consistency",
        ledger_ok,
        {"pair_count": len(pairs), "expected_jobs": len(expected_jobs), "pairs": pairs},
    )

    byte_equivalence_path = root / "byte-equivalence.json"
    if byte_equivalence_path.exists():
        equivalence = read_json(byte_equivalence_path)
        record("incremental_full_byte_equivalence", bool(equivalence.get("identical")), equivalence)

    ok = all(check["ok"] for check in checks)
    report = {"ok": ok, "check_count": len(checks), "checks": checks, "job_ids": expected_jobs}
    return report


def selfcheck_or_raise(state_dir: str | Path) -> dict[str, Any]:
    report = run_selfcheck(state_dir)
    if not report["ok"]:
        raise WorkbenchError(
            Codes.SELFCHECK_FAILED,
            "one or more consistency checks failed",
            {"failed_checks": [check["name"] for check in report["checks"] if not check["ok"]]},
        )
    return report
