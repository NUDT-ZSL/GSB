"""Cross-module consistency checks independent of cache bookkeeping."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .errors import E_SELFCHECK_FAILED


def run_selfcheck(
    manifest: dict[str, Any],
    quality_artifact: dict[str, Any],
    neardup_artifact: dict[str, Any],
    consistency_artifact: dict[str, Any],
    sampling_artifact: dict[str, Any],
    schedule_report: dict[str, Any],
    *,
    active_pair_ids: list[str],
    stage_audit: dict[str, Any],
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []

    def violation(code: str, message: str, **details: Any) -> None:
        violations.append({"code": code, "message": message, "details": details})

    active = set(active_pair_ids)
    records = manifest.get("records", [])
    manifest_ids = [str(record["pair_id"]) for record in records]
    manifest_counts = Counter(manifest_ids)
    duplicate_ids = sorted(pair_id for pair_id, count in manifest_counts.items() if count != 1)
    if duplicate_ids:
        violation("MANIFEST_DUPLICATE_PAIR", "manifest pairs must appear exactly once", pair_ids=duplicate_ids)
    if "input_pair_ids" in manifest:
        input_ids = set(str(pair_id) for pair_id in manifest["input_pair_ids"])
    else:
        input_ids = set(str(pair["pair_id"]) for pair in manifest.get("input_pairs", []))
    if input_ids and set(manifest_ids) != input_ids:
        violation(
            "MANIFEST_COVERAGE",
            "manifest records must cover every input pair",
            missing=sorted(input_ids - set(manifest_ids)),
            extra=sorted(set(manifest_ids) - input_ids),
        )

    for stage in ("quality", "neardup", "consistency", "sampling"):
        audit = stage_audit.get(stage, {})
        recomputed = set(audit.get("recomputed_pair_ids", []))
        reused = set(audit.get("reused_pair_ids", []))
        affected = set(audit.get("affected_pair_ids", []))
        if recomputed & reused:
            violation(
                "STAGE_REUSE_OVERLAP",
                "recomputed and reused pair sets must be disjoint",
                stage=stage,
                pair_ids=sorted(recomputed & reused),
            )
        if active and (recomputed | reused) != active:
            violation(
                "STAGE_COVERAGE",
                "recomputed and reused pair sets must cover all active pairs",
                stage=stage,
                missing=sorted(active - (recomputed | reused)),
                extra=sorted((recomputed | reused) - active),
            )
        if not affected.issubset(recomputed | reused):
            violation(
                "AFFECTED_NOT_COVERED",
                "affected pairs must be covered by recomputed or reused pairs",
                stage=stage,
                missing=sorted(affected - (recomputed | reused)),
            )

    cluster_assignments = neardup_artifact.get("assignments", {})
    if set(cluster_assignments) != active:
        violation(
            "NEARDUP_COVERAGE",
            "near-duplicate assignments must cover each active pair exactly once",
            missing=sorted(active - set(cluster_assignments)),
            extra=sorted(set(cluster_assignments) - active),
        )
    clusters = neardup_artifact.get("clusters", [])
    representatives = [cluster["representative_pair_id"] for cluster in clusters]
    if len(representatives) != len(set(representatives)):
        violation("NEARDUP_REPRESENTATIVE_DUPLICATE", "cluster representatives must be unique")
    cluster_members = [
        member for cluster in clusters for member in cluster.get("members", [])
    ]
    if len(cluster_members) != len(set(cluster_members)) or set(cluster_members) != active:
        violation(
            "NEARDUP_MEMBER_PARTITION",
            "every active pair must belong to exactly one cluster",
        )

    sequence = sampling_artifact.get("sequence", [])
    sequence_ids = [str(item["pair_id"]) for item in sequence]
    if len(sequence_ids) != len(set(sequence_ids)):
        violation("SAMPLING_DUPLICATE_SELECTION", "sampling sequence contains duplicate pairs")
    sequence_counts = Counter(str(item["quota_key"]) for item in sequence)
    for quota in sampling_artifact.get("quota_stats", []):
        if int(quota["selected_count"]) != sequence_counts[str(quota["quota_key"])]:
            violation(
                "SAMPLING_QUOTA_RECOUNT",
                "quota selected_count does not match sampling sequence",
                quota=quota["quota_key"],
            )
    if set(sequence_ids) != set(sampling_artifact.get("selected_pair_ids", [])):
        violation("SAMPLING_SELECTED_MISMATCH", "selected_pair_ids disagrees with sequence")
    if sum(int(quota["selected_count"]) for quota in sampling_artifact.get("quota_stats", [])) != len(
        sequence
    ):
        violation("SAMPLING_TOTAL_RECOUNT", "quota totals do not sum to sequence length")

    manifest_sampled = {
        str(record["pair_id"])
        for record in records
        if record.get("sampling", {}).get("selected")
    }
    if manifest_sampled != set(sequence_ids):
        violation(
            "MANIFEST_SAMPLING_MISMATCH",
            "manifest sampled pairs do not match sampling detail",
            missing=sorted(set(sequence_ids) - manifest_sampled),
            extra=sorted(manifest_sampled - set(sequence_ids)),
        )

    conflict_pairs = consistency_artifact.get("conflict_pairs", [])
    consistency_records = consistency_artifact.get("records", {})
    for conflict in conflict_pairs:
        for pair_id in conflict.get("pair_ids", []):
            record = consistency_records.get(str(pair_id))
            if not record or not record.get("conflicted"):
                violation(
                    "CONFLICT_EVIDENCE_MISSING",
                    "conflict participant lacks a conflicted pair record",
                    pair_id=pair_id,
                    conflict_id=conflict.get("conflict_id"),
                )

    pair_paths = schedule_report.get("pair_paths", {})
    if set(pair_paths) != input_ids:
        violation(
            "SCHEDULE_PATH_COVERAGE",
            "scheduler must record exactly one path for every input pair",
            missing=sorted(input_ids - set(pair_paths)),
            extra=sorted(set(pair_paths) - input_ids),
        )
    schedule_admitted = {
        pair_id for pair_id, path in pair_paths.items() if path.get("status") == "accepted"
    }
    selected_plan = schedule_report.get("selected_plan", {})
    if schedule_admitted != set(selected_plan.get("admitted_pair_ids", [])):
        violation(
            "SCHEDULE_ADMISSION_MISMATCH",
            "pair paths disagree with selected plan admission set",
        )

    quality_records = {
        str(record["pair_id"]): record for record in quality_artifact.get("records", [])
    }
    if set(quality_records) != active:
        violation(
            "QUALITY_COVERAGE",
            "quality artifact must cover every active pair",
            missing=sorted(active - set(quality_records)),
            extra=sorted(set(quality_records) - active),
        )
    if quality_records:
        quality_ranked = sorted(
            quality_records.values(),
            key=lambda record: (-float(record["pair_score"]), str(record["pair_id"])),
        )
        expected_ranks = {
            str(record["pair_id"]): index
            for index, record in enumerate(quality_ranked)
        }
        for pair_id, record in quality_records.items():
            if int(record.get("quality_rank", -1)) != expected_ranks[pair_id]:
                violation(
                    "QUALITY_RANK_MISMATCH",
                    "quality rank is not sorted by score desc and pair_id asc",
                    pair_id=pair_id,
                )

    return {
        "ok": not violations,
        "code": None if not violations else E_SELFCHECK_FAILED,
        "violations": violations,
        "checked": {
            "input_pair_count": len(input_ids),
            "active_pair_count": len(active),
            "manifest_record_count": len(records),
            "cluster_count": len(clusters),
            "conflict_pair_count": len(conflict_pairs),
            "selected_count": len(sequence),
        },
    }
