from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from .atomic import atomic_write_json, atomic_write_jsonl
from .config import InputBundle
from .hashing import sha256_json
from .snapshot import SNAPSHOT_SCHEMA, SNAPSHOT_VERSION


REPORT_VERSION = "1.0"


def _decision_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    decisions: dict[str, dict[str, Any]] = {}
    groups = state["dedupe"]["groups"]
    for group in groups:
        representative = group["representative_sample_id"]
        decisions[representative] = {
            "group_id": group["group_id"],
            "status": "retained",
            "representative_sample_id": representative,
        }
    for row in state["dedupe"]["removals"]:
        decisions[row["sample_id"]] = {
            "group_id": row["group_id"],
            "status": "removed",
            "representative_sample_id": row["representative_sample_id"],
            "representative_similarity": row["representative_similarity"],
        }
    return decisions


def _count_by_task(items: list[dict[str, Any]], task_key: str = "task_type") -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        task_type = str(item.get(task_key, "<unknown>"))
        counts[task_type] = counts.get(task_type, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def build_report(bundle: InputBundle, state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    candidates = sorted(state["generation"]["candidates"], key=lambda item: item["sample_id"])
    scored = sorted(state["scoring"]["scored"], key=lambda item: item["sample_id"])
    decisions = _decision_map(state)
    scored_by_id = {item["sample_id"]: item for item in scored}
    candidate_by_id = {item["sample_id"]: item for item in candidates}
    retained_rows: list[dict[str, Any]] = []
    removed_rows: list[dict[str, Any]] = []
    scored_rows: list[dict[str, Any]] = []
    for sample_id in sorted(scored_by_id):
        candidate = candidate_by_id.get(sample_id)
        if candidate is None:
            continue
        score = scored_by_id[sample_id]
        decision = decisions.get(
            sample_id,
            {
                "group_id": None,
                "status": "unclassified",
                "representative_sample_id": None,
            },
        )
        row = {
            **candidate,
            "score": score,
            "dedupe": decision,
        }
        scored_rows.append({"candidate": candidate, "score": score})
        if decision["status"] == "retained":
            retained_rows.append(row)
        elif decision["status"] == "removed":
            removed_rows.append(row)

    failures = sorted(
        state["failures"],
        key=lambda item: (
            str(item.get("sample_id", "")),
            str(item.get("stage", "")),
            str(item.get("business_code", "")),
            str(item.get("exception_type", "")),
        ),
    )
    injection_hits = [
        state["injection_hits"][key] for key in sorted(state["injection_hits"])
    ]
    precision_sensitive = [
        {
            "sample_id": item["sample_id"],
            "task_type": item["task_type"],
            "total_fp32": item["fp32"]["total_score"],
            "total_fp64": item["fp64"]["total_score"],
            "total_delta": item["delta"]["total_score"],
            "threshold": item["threshold"],
            "first_over_threshold_stage": item["first_over_threshold_stage"],
            "injection_ids": item["injection_ids"],
            "error_propagation": item["error_propagation"],
        }
        for item in scored
        if item["precision_sensitive"]
    ]
    boundary_diagnostics = sorted(
        state["dedupe"]["length_boundary"] + state["dedupe"]["similarity_boundary"],
        key=lambda item: item["diagnostic_id"],
    )
    divergence_ids = sorted(
        {
            item["diagnostic_id"]
            for item in boundary_diagnostics
            if item.get("business_code") == "E_PRECISION_DIVERGENCE"
        }
    )

    report: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "status": "completed",
        "input_fingerprint": bundle.input_fingerprint,
        "generation_fingerprint": bundle.generation_fingerprint,
        "config_hash": bundle.config_hash,
        "scoring_params_hash": bundle.scoring_params_hash,
        "injection_hash": bundle.injection_hash,
        "precision_mode": bundle.precision_mode,
        "configured_concurrency": bundle.concurrency,
        "precision_error_threshold": bundle.precision_error_threshold,
        "boundary_epsilon": bundle.boundary_epsilon,
        "dedupe_similarity_threshold": bundle.config["dedupe_similarity_threshold"],
        "summary": {
            "generated_candidates": len(candidates),
            "scored_candidates": len(scored),
            "retained_samples": len(retained_rows),
            "removed_samples": len(removed_rows),
            "failed_candidates": len(failures),
            "precision_sensitive_samples": len(precision_sensitive),
            "boundary_diagnostics": len(boundary_diagnostics),
            "precision_divergences": len(divergence_ids),
            "injection_items": len(bundle.injections.get("items", [])),
            "injection_hits": len([hit for hit in injection_hits if hit.get("outcome") == "applied"]),
            "actual_sample_count_by_task_type": _count_by_task(retained_rows),
            "generated_count_by_task_type": _count_by_task(candidates),
            "removed_count_by_task_type": _count_by_task(removed_rows),
        },
        "precision_sensitive": precision_sensitive,
        "boundary_diagnostics": boundary_diagnostics,
        "divergence_diagnostic_ids": divergence_ids,
        "removed": sorted(state["dedupe"]["removals"], key=lambda item: item["sample_id"]),
        "groups": sorted(state["dedupe"]["groups"], key=lambda item: item["group_id"]),
        "failures": failures,
        "generation_skips": sorted(
            state["generation"]["skips"],
            key=lambda item: (
                item["task_type"],
                item["reason_code"],
                str(item.get("template_variant_id") or ""),
            ),
        ),
        "injection_hits": injection_hits,
        "snapshot": {
            "schema": SNAPSHOT_SCHEMA,
            "version": SNAPSHOT_VERSION,
            "stage": state["snapshot_stage"],
            "cursor": state["cursor"],
            "summary": state.get("result_summary", {}),
        },
        "stage_timings": {
            key: round(float(value), 9)
            for key, value in sorted(state["stage_timings"].items())
        },
    }
    report_core = deepcopy(report)
    report_core.pop("stage_timings", None)
    report["canonical_report_fingerprint"] = sha256_json(report_core)

    return report, {
        "candidates": candidates,
        "scored_rows": scored_rows,
        "retained_rows": retained_rows,
        "removed_rows": removed_rows,
        "failures": failures,
        "diagnostics": boundary_diagnostics,
    }


def write_outputs(
    bundle: InputBundle, state: dict[str, Any], output_dir: Path
) -> tuple[dict[str, Any], dict[str, Any]]:
    report, rows = build_report(bundle, state)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_jsonl(output_dir / "candidates.jsonl", rows["candidates"])
    atomic_write_jsonl(output_dir / "scored.jsonl", rows["scored_rows"])
    atomic_write_jsonl(output_dir / "retained.jsonl", rows["retained_rows"])
    atomic_write_jsonl(output_dir / "removed.jsonl", rows["removed_rows"])
    atomic_write_jsonl(output_dir / "failures.jsonl", rows["failures"])
    atomic_write_jsonl(output_dir / "diagnostics.jsonl", rows["diagnostics"])
    atomic_write_json(output_dir / "report.json", report)
    return report, rows

