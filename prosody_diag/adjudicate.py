from __future__ import annotations

from typing import Any

from .canonical import canonical_hash
from .config import Config
from .errors import E_CONFLICT_OVERFLOW, DiagnosticError
from .precision import PrecisionContext
from .trees import infer_candidate


LABEL_PRIORITY = {"truth": 0, "v2": 1, "v1": 2}


def truth_key(utterance_id: str, frame_index: int, boundary_type: str) -> tuple[str, int, str]:
    return utterance_id, int(frame_index), str(boundary_type)


def index_boundary_truth(rows: list[dict[str, Any]]) -> dict[tuple[str, int, str], dict[str, Any]]:
    index: dict[tuple[str, int, str], dict[str, Any]] = {}
    for row in rows:
        key = truth_key(row["utterance_id"], row["frame_index"], row["boundary_type"])
        index[key] = row
    return index


def merge_label(record: dict[str, Any]) -> dict[str, Any]:
    """Apply explicit truth > v2 > v1 and same-source rule-number order."""

    truth = record.get("truth")
    v2 = record.get("v2")
    v1 = record.get("v1")
    if truth and truth.get("gold_label") is not None:
        return {
            "label": truth["gold_label"],
            "decided_by": "truth",
            "rule_id": "REFERENCE",
            "confidence": 1.0,
            "priority_rank": LABEL_PRIORITY["truth"],
        }
    if v2:
        return {
            "label": v2["label"],
            "decided_by": "v2",
            "rule_id": v2["selected_rule_id"],
            "confidence": v2["confidence"],
            "priority_rank": LABEL_PRIORITY["v2"],
        }
    if v1:
        return {
            "label": v1["label"],
            "decided_by": "v1",
            "rule_id": v1["selected_rule_id"],
            "confidence": v1["confidence"],
            "priority_rank": LABEL_PRIORITY["v1"],
        }
    return {
        "label": "B0",
        "decided_by": "default",
        "rule_id": "DEFAULT",
        "confidence": 0.0,
        "priority_rank": 99,
    }


def judge_candidate(
    task: dict[str, Any],
    candidate: dict[str, Any],
    trees: dict[str, dict[str, Any]],
    truth_index: dict[tuple[str, int, str], dict[str, Any]],
    profile_name: str,
    cfg: Config,
) -> dict[str, Any]:
    context = PrecisionContext(profile_name)
    v1_result = infer_candidate(trees["v1"], candidate, context, cfg) if trees.get("v1") else None
    v2_result = infer_candidate(trees["v2"], candidate, context, cfg) if trees.get("v2") else None
    truth_row = truth_index.get(
        truth_key(task["utterance_id"], candidate["frame_index"], candidate["boundary_type"])
    )
    truth_payload = (
        {
            "gold_label": truth_row["gold_label"],
            "pause_duration_ms": truth_row.get("pause_duration_ms"),
            "source": "boundary_truth.jsonl",
        }
        if truth_row
        else None
    )
    core = {
        "utterance_id": task["utterance_id"],
        "candidate_id": candidate["candidate_id"],
        "frame_index": candidate["frame_index"],
        "boundary_type": candidate["boundary_type"],
        "precision_profile": context.spec_hash_object(),
        "truth": truth_payload,
        "v1": v1_result,
        "v2": v2_result,
    }
    merged = merge_label(core)
    conflict = None
    if v1_result and v2_result and v1_result["label"] != v2_result["label"]:
        conflict = {
            "conflict_key": candidate["candidate_id"],
            "frame_index": candidate["frame_index"],
            "boundary_type": candidate["boundary_type"],
            "v1_label": v1_result["label"],
            "v2_label": v2_result["label"],
            "adjudicated_by": merged["decided_by"],
            "adjudicated_label": merged["label"],
            "rule_number_order": "lower rule number wins within one source version",
            "priority_order": "truth > v2 > v1",
        }
    core["merged"] = merged
    core["version_conflict"] = conflict
    core["record_hash"] = canonical_hash(
        {key: value for key, value in core.items() if key not in {"record_hash"}}
    )
    return core


def enforce_conflict_threshold(judgments: list[dict[str, Any]], threshold: int) -> list[dict[str, Any]]:
    conflicts: dict[str, dict[str, Any]] = {}
    for judgment in judgments:
        conflict = judgment.get("version_conflict")
        if conflict:
            conflicts[conflict["conflict_key"]] = conflict
    if len(conflicts) > threshold:
        raise DiagnosticError(
            E_CONFLICT_OVERFLOW,
            "cross-version conflict candidate count exceeds threshold",
            field="conflict_threshold",
            details={
                "conflict_count": len(conflicts),
                "threshold": threshold,
                "conflicts": sorted(conflicts.values(), key=lambda item: item["conflict_key"]),
            },
        )
    return sorted(conflicts.values(), key=lambda item: item["conflict_key"])


def judgments_to_ledger_rows(judgments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for judgment in judgments:
        merged = merge_label(judgment)
        profile = judgment["precision_profile"]
        v1 = judgment.get("v1") or {}
        v2 = judgment.get("v2") or {}
        rows.append(
            {
                "ledger_key": canonical_hash(
                    {
                        "utterance_id": judgment["utterance_id"],
                        "candidate_id": judgment["candidate_id"],
                        "precision_name": profile["name"],
                    }
                )[:20],
                "utterance_id": judgment["utterance_id"],
                "candidate_id": judgment["candidate_id"],
                "frame_index": judgment["frame_index"],
                "boundary_type": judgment["boundary_type"],
                "precision_profile": profile["name"],
                "precision_spec_hash": canonical_hash(profile)[:16],
                "v1_label": v1.get("label"),
                "v1_confidence": v1.get("confidence"),
                "v1_rule_id": v1.get("selected_rule_id"),
                "v1_tree_hash": v1.get("tree_hash"),
                "v2_label": v2.get("label"),
                "v2_confidence": v2.get("confidence"),
                "v2_rule_id": v2.get("selected_rule_id"),
                "v2_tree_hash": v2.get("tree_hash"),
                "truth_label": (judgment.get("truth") or {}).get("gold_label"),
                "merged_label": merged["label"],
                "decided_by": merged["decided_by"],
                "merged_rule_id": merged["rule_id"],
                "has_version_conflict": bool(judgment.get("version_conflict")),
                "record_hash": judgment["record_hash"],
            }
        )
    rows.sort(
        key=lambda row: (
            row["utterance_id"],
            row["frame_index"],
            row["boundary_type"],
            row["precision_profile"],
        )
    )
    return rows


def recompute_ledger_from_judgments(judgments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return judgments_to_ledger_rows(judgments)
