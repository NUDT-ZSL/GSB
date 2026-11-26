from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .canonical import content_digest
from .errors import E_INCONSISTENT, E_NOT_FOUND, TuningError
from .ledger import SnapshotStore
from .planner import plan_search
from .workload import compute_quant_report


def assert_partition(
    recompute: list[str] | set[str],
    reuse: list[str] | set[str],
    affected: list[str] | set[str] | None = None,
    universe: list[str] | set[str] | None = None,
    label: str = "propagation",
) -> None:
    recompute_set = set(recompute)
    reuse_set = set(reuse)
    overlap = sorted(recompute_set & reuse_set)
    if overlap:
        raise TuningError(
            E_INCONSISTENT,
            f"{label}: recompute and reuse sets overlap",
            {"overlap": overlap},
        )
    union = recompute_set | reuse_set
    target = set(affected if affected is not None else universe or [])
    if affected is not None and union != target:
        missing = sorted(target - union)
        extra = sorted(union - target)
        raise TuningError(
            E_INCONSISTENT,
            f"{label}: recompute/reuse union does not cover affected set",
            {"missing": missing, "extra": extra},
        )
    if universe is not None and union != set(universe):
        raise TuningError(
            E_INCONSISTENT,
            f"{label}: recompute/reuse union does not cover universe",
            {
                "missing": sorted(set(universe) - union),
                "extra": sorted(union - set(universe)),
            },
        )


def verify_propagation_document(document: dict[str, Any]) -> dict[str, Any]:
    assert_partition(
        document["recompute_set"],
        document["reuse_set"],
        affected=document["affected_set"],
        label="affected closure",
    )
    assert_partition(
        document["full_recompute_set"],
        document["full_reuse_set"],
        universe=document["coverage_universe"],
        label="full coverage",
    )
    point_overlap = set(document["recompute_points"]) & set(document["reuse_points"])
    if point_overlap:
        raise TuningError(E_INCONSISTENT, "point recompute/reuse overlap")
    return {
        "affected_count": len(document["affected_set"]),
        "recompute_count": len(document["recompute_set"]),
        "reuse_count": len(document["reuse_set"]),
    }


def self_check(
    output_dir: str | Path,
    tasks: list[dict[str, Any]],
    platform: dict[str, Any],
    policy: dict[str, Any],
    propagation_path: str | Path | None = None,
) -> dict[str, Any]:
    store = SnapshotStore(output_dir)
    record = store.load_latest()
    if record is None:
        raise TuningError(E_NOT_FOUND, "no ledger checkpoint found")

    versions = store.list_versions()
    if versions != list(range(1, len(versions) + 1)):
        raise TuningError(E_INCONSISTENT, "checkpoint versions are not contiguous", {"versions": versions})
    for version in versions:
        store.load_record(store.versioned_checkpoint(version))

    payload = record["payload"]
    expected_digest = content_digest(
        {"tasks": tasks, "platform": platform, "policy": policy}
    )
    if payload.get("input_digest") != expected_digest:
        raise TuningError(E_INCONSISTENT, "input digest mismatch")

    plan = plan_search(tasks, platform, policy)
    plan_index = plan["point_index"]
    batch_index = {batch["batch_no"]: batch for batch in plan["batches"]}
    results = payload.get("results", [])
    seen: set[str] = set()

    for result in results:
        point_id = result["point_id"]
        if point_id in seen:
            raise TuningError(E_INCONSISTENT, "duplicate committed point", {"point_id": point_id})
        seen.add(point_id)
        if point_id not in plan_index:
            raise TuningError(E_INCONSISTENT, "ledger point absent from deterministic plan", {"point_id": point_id})
        point = plan_index[point_id]
        if result["sequence_no"] != point["sequence_no"]:
            raise TuningError(E_INCONSISTENT, "sequence number mismatch", {"point_id": point_id})
        batch_no = result["batch_no"]
        batch = batch_index.get(batch_no)
        if batch is None or point_id not in batch["point_ids"]:
            raise TuningError(E_INCONSISTENT, "point/batch assignment mismatch", {"point_id": point_id})
        if int(result["measured"]["elapsed_ns_median"]) <= 0:
            raise TuningError(E_INCONSISTENT, "actual elapsed time missing", {"point_id": point_id})

        recomputed = compute_quant_report(
            result["tuning_id"],
            result["op_kind"],
            point["workload_shape"],
            point["quant_config"],
            int(result["effective_bits"]),
        )
        if recomputed["metrics_digest"] != result["quant_metrics_digest"]:
            raise TuningError(
                E_INCONSISTENT,
                "quantization metrics cannot be independently reproduced",
                {"point_id": point_id},
            )
        if content_digest(recomputed["metrics"]) != result["quant_report"]["metrics_digest"]:
            raise TuningError(E_INCONSISTENT, "stored quant metrics digest is inconsistent")

    for batch_no in payload.get("committed_batches", []):
        batch = batch_index.get(int(batch_no))
        if batch is None:
            raise TuningError(E_INCONSISTENT, "unknown committed batch", {"batch_no": batch_no})
        batch_ids = set(batch["point_ids"])
        ledger_batch_ids = {
            result["point_id"] for result in results if result["batch_no"] == batch_no
        }
        if batch_ids != ledger_batch_ids:
            raise TuningError(
                E_INCONSISTENT,
                "ledger batch does not match scheduler batch",
                {
                    "batch_no": batch_no,
                    "missing": sorted(batch_ids - ledger_batch_ids),
                    "extra": sorted(ledger_batch_ids - batch_ids),
                },
            )

    if payload.get("next_batch_no") != max(payload.get("committed_batches", [0]), default=0) + 1:
        # Resume may stop at a boundary only when next points exist; invariant is
        # next_batch_no = last committed batch + 1.
        committed = payload.get("committed_batches", [])
        if committed and payload.get("next_batch_no") != max(committed) + 1:
            raise TuningError(E_INCONSISTENT, "checkpoint next_batch_no is inconsistent")

    inflight = list(store.work_dir.glob("*.inflight"))
    if inflight:
        raise TuningError(E_INCONSISTENT, "stale inflight marker remains after recovery/verification")

    report_path = Path(output_dir) / "reports" / "run-report.json"
    if report_path.exists():
        report = json.loads(report_path.read_text(encoding="utf-8"))
        report_ids = {
            item["point_id"]
            for items in report.get("rankings", {}).values()
            for item in items
        }
        if not report_ids.issubset(seen):
            raise TuningError(
                E_INCONSISTENT,
                "exit report contains uncommitted evaluation points",
                {"unknown": sorted(report_ids - seen)},
            )

    propagation_summary = None
    prop_path = Path(propagation_path) if propagation_path else Path(output_dir) / "propagation.json"
    if prop_path.exists():
        propagation_document = json.loads(prop_path.read_text(encoding="utf-8"))
        propagation_summary = verify_propagation_document(propagation_document)

    return {
        "ok": True,
        "latest_version": record["snapshot_version"],
        "checkpoint_versions": versions,
        "committed_points": len(seen),
        "committed_batches": payload.get("committed_batches", []),
        "propagation": propagation_summary,
        "checks": [
            "snapshot_checksum",
            "monotonic_checkpoint_versions",
            "point_batch_consistency",
            "independent_quant_recompute",
            "report_commit_subset",
            "propagation_partition",
        ],
    }
