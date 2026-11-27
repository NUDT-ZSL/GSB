"""CLI-facing workload runner, edge-case checks, and restart verification."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .cache import CompileCacheEngine
from .errors import BusinessRuleError
from .graph import project_fingerprint
from .util import atomic_write_json
from .validation import validate_project


def build_timeline(project: Mapping[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for event in project.get("events", []):
        items.append({"kind": "event", "time": float(event["time"]), "item": event})
    for request in project.get("requests", []):
        items.append({"kind": "request", "time": float(request["time"]), "item": request})
    items.sort(
        key=lambda entry: (
            entry["time"],
            0 if entry["kind"] == "event" else 1,
            str(entry["item"].get("id", "")),
        )
    )
    return items


def choose_cutoff(timeline: Sequence[Mapping[str, Any]], requested: int | None) -> int:
    if requested is not None:
        return max(0, min(len(timeline), int(requested)))
    for index, entry in enumerate(timeline):
        if entry["kind"] == "event" and entry["item"].get("type") == "model_update":
            # Snapshot after the update and a few following requests so the
            # restart path exercises both the updated model and retained cache
            # state from the other model.
            return min(len(timeline), index + 5)
    if not timeline:
        return 0
    return max(1, int(len(timeline) * 0.62))


def process_timeline(
    engine: CompileCacheEngine,
    timeline: Sequence[Mapping[str, Any]],
    *,
    start: int = 0,
    end: int | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    stop = len(timeline) if end is None else min(len(timeline), end)
    for entry in timeline[start:stop]:
        if entry["kind"] == "event":
            records.append(engine.apply_event(entry["item"]))
        else:
            records.append(engine.process_request(entry["item"]))
    return records


def _record_signature(record: Mapping[str, Any]) -> dict[str, Any]:
    if record.get("kind") == "event":
        return {
            "kind": "event",
            "event_id": record.get("event_id"),
            "status": record.get("status"),
            "invalidated_keys": record.get("invalidated_keys"),
            "incremental_recompile_scope": record.get("incremental_recompile_scope"),
            "recompiled_keys": [
                {
                    "old_compile_key": item.get("old_compile_key"),
                    "new_compile_key": item.get("new_compile_key"),
                    "compiled_units": item.get("compiled_units"),
                    "reused_units": item.get("reused_units"),
                    "artifact_ids": item.get("artifact_ids"),
                    "unaffected_artifact_ids_stable": item.get(
                        "unaffected_artifact_ids_stable"
                    ),
                }
                for item in record.get("recompiled_keys", [])
            ],
            "unaffected_artifact_ids_stable": record.get(
                "unaffected_artifact_ids_stable"
            ),
            "evictions": [item.get("compile_key") for item in record.get("evictions", [])],
        }
    return {
        "kind": "request",
        "request_id": record.get("request_id"),
        "status": record.get("status"),
        "compile_key": record.get("compile_key"),
        "cache_lookup": record.get("cache_lookup"),
        "cache_hit": record.get("cache_hit"),
        "artifact_ids": record.get("artifact_ids"),
        "compiled_units": (record.get("compile") or {}).get("compiled_units"),
        "reused_units": (record.get("compile") or {}).get("reused_units"),
        "evictions": [item.get("compile_key") for item in record.get("evictions", [])],
        "outputs": {
            name: value.get("fingerprint")
            for name, value in (record.get("outputs") or {}).items()
        },
        "numeric_consistent": record.get("numeric_consistent"),
    }


def compare_restart_suffix(
    uninterrupted: Sequence[Mapping[str, Any]],
    restored: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    left = [_record_signature(record) for record in uninterrupted]
    right = [_record_signature(record) for record in restored]
    mismatch_indexes = []
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            mismatch_indexes.append(
                {
                    "index": index,
                    "request_id": a.get("request_id") or a.get("event_id"),
                    "uninterrupted": a,
                    "restored": b,
                }
            )
    hit_checks = []
    key_checks = []
    artifact_checks = []
    eviction_checks = []
    for a, b in zip(left, right):
        if a["kind"] != "request":
            continue
        hit_checks.append(a.get("cache_hit") == b.get("cache_hit"))
        key_checks.append(a.get("compile_key") == b.get("compile_key"))
        artifact_checks.append(a.get("artifact_ids") == b.get("artifact_ids"))
        eviction_checks.append(a.get("evictions") == b.get("evictions"))
    return {
        "suffix_record_count": len(left),
        "records_identical": not mismatch_indexes and len(left) == len(right),
        "hit_decisions_identical": all(hit_checks) and len(hit_checks) > 0,
        "compile_keys_identical": all(key_checks) and len(key_checks) > 0,
        "artifact_ids_identical": all(artifact_checks) and len(artifact_checks) > 0,
        "eviction_decisions_identical": all(eviction_checks) and len(eviction_checks) > 0,
        "mismatches": mismatch_indexes,
    }


def _jsonl_write(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    with open(temporary, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
        handle.flush()
    temporary.replace(path)


def run_project(
    project: Mapping[str, Any],
    *,
    out_dir: str | Path = "artifacts/run",
    cutoff_index: int | None = None,
    write_snapshots: bool = True,
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    validation = validate_project(project)
    timeline = build_timeline(project)
    cutoff = choose_cutoff(timeline, cutoff_index)

    engine = CompileCacheEngine(project)
    prefix_records = process_timeline(engine, timeline, start=0, end=cutoff)
    checkpoint_path = out / "cache_checkpoint.json"
    final_path = out / "cache_final.json"
    latest_path = out / "latest_snapshot.json"
    checkpoint_payload = (
        engine.write_snapshot(checkpoint_path) if write_snapshots else engine.snapshot_payload()
    )
    suffix_records = process_timeline(engine, timeline, start=cutoff, end=len(timeline))
    final_payload = engine.write_snapshot(final_path) if write_snapshots else engine.snapshot_payload()
    if write_snapshots:
        atomic_write_json(latest_path, final_payload)

    restored = CompileCacheEngine.restore(checkpoint_path, project) if write_snapshots else None
    if restored is not None:
        restored_suffix = process_timeline(
            restored, timeline, start=cutoff, end=len(timeline)
        )
        restart_check = compare_restart_suffix(suffix_records, restored_suffix)
        suffix_event_records = [
            record for record in suffix_records if record.get("kind") == "event"
        ]
        restored_event_records = [
            record for record in restored_suffix if record.get("kind") == "event"
        ]
        restart_check["event_reports_identical"] = [
            _record_signature(record) for record in suffix_event_records
        ] == [_record_signature(record) for record in restored_event_records]
        restart_check["restore_report"] = restored.restore_report
    else:
        restart_check = {
            "suffix_record_count": len(suffix_records),
            "records_identical": False,
            "hit_decisions_identical": False,
            "compile_keys_identical": False,
            "artifact_ids_identical": False,
            "eviction_decisions_identical": False,
            "mismatches": [{"reason": "snapshots disabled"}],
            "event_reports_identical": False,
            "restore_report": None,
        }

    summary = engine.summary()
    expectation_checks = _expectation_checks(project, engine)
    numeric_mismatches = [
        record.get("mismatch")
        for record in engine.request_records
        if record.get("status") == "accepted" and not record.get("numeric_consistent", True)
    ]
    distinct_request_keys = {
        record["compile_key"]
        for record in engine.request_records
        if record.get("status") == "accepted"
    }
    expectation_distinct = int(
        project.get("expectations", {}).get("distinct_compile_key_count", len(distinct_request_keys))
    )
    shape_distribution = summary["distribution_by_model_version"]
    shape_distribution_ok = _distribution_matches_expectations(project, shape_distribution)
    acceptance = {
        "validation_has_no_fatal_errors": bool(validation.get("executable")),
        "expected_error_codes_match": expectation_checks["expected_error_codes_match"],
        "all_valid_requests_accepted": summary["valid_requests"]
        == int(project.get("expectations", {}).get("valid_request_count", summary["valid_requests"])),
        "rejected_requests_match_oracle": summary["rejected_requests"]
        == int(project.get("expectations", {}).get("invalid_request_count", summary["rejected_requests"])),
        "distinct_request_keys_match_oracle": len(distinct_request_keys)
        == expectation_distinct,
        "shape_distribution_matches_oracle": shape_distribution_ok,
        "at_least_one_eviction": summary["eviction_count"] > 0,
        "at_least_one_shape_specialization": summary["shape_specialization_events"] > 0,
        "at_least_one_model_update": len(summary["events"]) > 0,
        "all_numeric_outputs_match_baseline": len(numeric_mismatches) == 0,
        "restart_hit_decisions_identical": restart_check["hit_decisions_identical"],
        "restart_records_identical": restart_check["records_identical"],
        "snapshot_restore_did_not_full_compile": (
            (restart_check.get("restore_report") or {}).get(
                "full_compilations_during_restore", 1
            )
            == 0
        ),
        "shape_specialization_count_matches_request_misses": summary[
            "shape_specialization_events"
        ]
        == summary["cache_misses"],
    }
    report = {
        "schema_version": "flight-run-report/v1",
        "project": {
            "project_id": project.get("project_id"),
            "project_fingerprint": project_fingerprint(project),
            "seed": project.get("generator", {}).get("seed"),
            "cache_config": project.get("cache_config"),
        },
        "validation": validation,
        "timeline": {
            "item_count": len(timeline),
            "event_count": sum(1 for item in timeline if item["kind"] == "event"),
            "request_count": sum(1 for item in timeline if item["kind"] == "request"),
            "cutoff_index": cutoff,
            "prefix_records": len(prefix_records),
            "suffix_records": len(suffix_records),
        },
        "summary": summary,
        "requests": engine.request_records,
        "events": engine.event_log,
        "shape_specialization": {
            "events": summary["shape_specialization_events"],
            "kernel_compiles": summary["shape_specialized_kernel_compiles"],
            "novel_request_keys": summary["request_novel_key_compiles"],
            "recompiles_after_eviction": summary[
                "request_recompiles_after_eviction"
            ],
            "event_incremental_compiles": summary["event_incremental_compiles"],
            "request_key_distribution": summary["request_key_distribution"],
            "distribution_by_model_version": summary["distribution_by_model_version"],
            "oracle_distinct_key_count": expectation_distinct,
            "oracle_distribution": project.get("expectations", {}).get(
                "shape_segment_distribution", []
            ),
            "counts_match_request_shape_sequence": acceptance[
                "distinct_request_keys_match_oracle"
            ]
            and acceptance["shape_distribution_matches_oracle"],
        },
        "full_compile_baseline": {
            "full_compile_count": summary["baseline_full_compile_count"],
            "compile_work": summary["baseline_compile_work"],
            "compile_ms": summary["baseline_compile_ms"],
            "specialized_compile_work": summary["total_compile_work"],
            "specialized_compile_ms": summary["total_compile_ms"],
            "saved_compile_work": summary["saved_compile_work"],
            "work_reduction_ratio": summary["work_reduction_ratio"],
            "numeric_mismatches": numeric_mismatches,
        },
        "snapshots": {
            "checkpoint_path": str(checkpoint_path) if write_snapshots else None,
            "checkpoint_snapshot_id": checkpoint_payload.get("snapshot_id"),
            "final_path": str(final_path) if write_snapshots else None,
            "final_snapshot_id": final_payload.get("snapshot_id"),
            "latest_path": str(latest_path) if write_snapshots else None,
            "atomic_whole_file_replace": True,
            "snapshot_schema": checkpoint_payload.get("snapshot_schema"),
        },
        "restart_verification": restart_check,
        "expectation_checks": expectation_checks,
        "acceptance_checks": acceptance,
        "overall_ok": all(acceptance.values()),
    }
    if write_snapshots:
        atomic_write_json(out / "report.json", report)
        _jsonl_write(out / "request_log.jsonl", engine.request_records)
        _jsonl_write(out / "event_log.jsonl", engine.event_log)
        atomic_write_json(out / "restart_verification.json", restart_check)
    return report


def _expectation_checks(
    project: Mapping[str, Any], engine: CompileCacheEngine
) -> dict[str, Any]:
    expected = project.get("expectations", {}).get("request_expectations", {})
    observed = {}
    mismatches = []
    for record in engine.request_records:
        if record.get("kind") != "request":
            continue
        request_id = record.get("request_id")
        expected_item = expected.get(request_id, {})
        observed_item = {
            "valid": record.get("status") == "accepted",
            "error_code": record.get("error_code"),
        }
        observed[request_id] = observed_item
        if expected_item and (
            bool(expected_item.get("valid")) != observed_item["valid"]
            or expected_item.get("error_code") != observed_item["error_code"]
        ):
            mismatches.append(
                {
                    "request_id": request_id,
                    "expected": expected_item,
                    "observed": observed_item,
                }
            )
    return {
        "expected_error_codes_match": not mismatches,
        "mismatches": mismatches,
        "observed": observed,
    }


def _distribution_matches_expectations(
    project: Mapping[str, Any], distribution: Mapping[str, Any]
) -> bool:
    expected_rows = project.get("expectations", {}).get("shape_segment_distribution", [])
    observed: dict[tuple[str, str, str], int] = {}
    for group in distribution.values():
        for segment, bucket in group.get("shape_segments", {}).items():
            observed[(group["model_id"], group["model_version"], segment)] = int(
                bucket.get("request_occurrences", 0)
            )
    for row in expected_rows:
        key = (row["model_id"], row["model_version"], row["shape_segment"])
        if observed.get(key, 0) != int(row["request_count"]):
            return False
    return True


def run_edge_project(
    project: Mapping[str, Any], *, out_dir: str | Path = "artifacts/edge"
) -> dict[str, Any]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    validation = validate_project(project)
    engine = CompileCacheEngine(project)
    records = []
    for request in project.get("requests", []):
        records.append(engine.process_request(request))
    expected = project.get("expectations", {}).get("request_expectations", {})
    mismatches = []
    for record in records:
        request_id = record.get("request_id")
        expected_item = expected.get(request_id, {})
        observed = {
            "valid": record.get("status") == "accepted",
            "error_code": record.get("error_code"),
        }
        if expected_item and (
            bool(expected_item.get("valid")) != observed["valid"]
            or expected_item.get("error_code") != observed["error_code"]
        ):
            mismatches.append(
                {"request_id": request_id, "expected": expected_item, "observed": observed}
            )
    model_error_codes = {
        model_id: [error["code"] for error in model_report["errors"]]
        for model_id, model_report in validation.get("models", {}).items()
        if not model_report.get("ok", False)
    }
    report = {
        "schema_version": "flight-edge-report/v1",
        "project_id": project.get("project_id"),
        "validation": validation,
        "requests": records,
        "summary": engine.summary(),
        "expectation_mismatches": mismatches,
        "all_expected_outcomes_match": not mismatches,
        "model_error_codes": model_error_codes,
    }
    atomic_write_json(out / "edge_report.json", report)
    _jsonl_write(out / "edge_request_log.jsonl", records)
    return report
