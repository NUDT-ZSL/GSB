"""Full deterministic analysis and semantic report normalization."""

from __future__ import annotations

from typing import Any, Iterable

from . import CHECKPOINT_STRUCTURE_VERSION, MERGE_SEMANTICS_VERSION
from .canonical import canonical_json, stable_hash
from .config import EngineConfig
from .lineage import build_lineage_view
from .models import EventRecord, LineageRecord, PolicyRecord, Rejection
from .policies import build_policy_view, policy_summary
from .runtime import UnitJob, run_jobs


SEMANTIC_REPORT_KEYS = (
    "schema_version",
    "semantics_version",
    "rejections",
    "read_errors",
    "units",
    "queue_rejections",
    "lineage_issues",
    "forks",
    "missing_parent_issues",
    "cycle_issues",
    "self_reference_issues",
    "policy_conflicts",
    "tombstone_overrides",
    "policies",
)


def rejection_dicts(rejections: Iterable[Rejection | dict]) -> list[dict]:
    items = [
        item.to_dict() if isinstance(item, Rejection) else dict(item)
        for item in rejections
    ]
    return sorted(
        items,
        key=lambda item: (
            item.get("source_category", ""),
            item.get("source_name", ""),
            item.get("record_seq", -1),
            item.get("code", ""),
            canonical_json(item.get("field_errors", [])),
        ),
    )


def build_unit_jobs(events: list[EventRecord], lineage_view, policy_view) -> list[UnitJob]:
    groups: dict[str, tuple[Any, list[EventRecord]]] = {}
    for event in events:
        assignment = lineage_view.assignments[event.process_guid]
        if assignment.unit_id not in groups:
            groups[assignment.unit_id] = (assignment, [])
        groups[assignment.unit_id][1].append(event)

    return [
        UnitJob(assignment=assignment, events=group_events, policies=policy_view.policies)
        for unit_id, (assignment, group_events) in sorted(groups.items())
    ]


def analyze(
    events: list[EventRecord],
    lineage_records: list[LineageRecord],
    policy_records: list[PolicyRecord],
    config: EngineConfig | None = None,
    *,
    rejections: list[Rejection] | None = None,
    read_errors: list[dict] | None = None,
) -> dict[str, Any]:
    config = config or EngineConfig()
    lineage_view = build_lineage_view(events, lineage_records)
    policy_view = build_policy_view(policy_records)
    jobs = build_unit_jobs(events, lineage_view, policy_view)
    units, queue_rejections, timings = run_jobs(jobs, config)
    return assemble_report(
        events=events,
        lineage_records=lineage_records,
        policy_records=policy_records,
        lineage_view=lineage_view,
        policy_view=policy_view,
        units=units,
        queue_rejections=queue_rejections,
        timings=timings,
        config=config,
        rejections=rejections,
        read_errors=read_errors,
    )


def assemble_report(
    *,
    events: list[EventRecord],
    lineage_records: list[LineageRecord],
    policy_records: list[PolicyRecord],
    lineage_view,
    policy_view,
    units: list[dict[str, Any]],
    queue_rejections: list[dict[str, Any]],
    timings: dict[str, float],
    config: EngineConfig,
    rejections: list[Rejection] | None = None,
    read_errors: list[dict] | None = None,
    extra: dict[str, Any] | None = None,
    job_count: int | None = None,
) -> dict[str, Any]:
    units.sort(key=lambda unit: unit["unit_id"])
    queue_rejections.sort(key=lambda item: item.get("unit_id", ""))

    rejection_items = rejection_dicts(rejections or [])
    read_error_items = sorted(
        read_errors or [],
        key=lambda item: (
            item.get("source_category", ""),
            item.get("source_name", ""),
            item.get("record_seq", -1),
            item.get("code", ""),
        ),
    )

    hit_count = sum(len(unit.get("hits", [])) for unit in units)
    decision_count = sum(1 for unit in units if unit.get("decision"))
    timeout_count = sum(
        1 for unit in units if any(error.get("code") == "E_TIMEOUT" for error in unit.get("errors", []))
    )
    budget_count = sum(
        1 for unit in units if any(error.get("code") == "E_BUDGET" for error in unit.get("errors", []))
    )
    rejected_record_keys = {
        f"{item['source_category']}:{item['source_name']}:{item['record_seq']}"
        for item in rejection_items
        if item.get("record_seq", 0) > 0
    }

    report: dict[str, Any] = {
        "schema_version": CHECKPOINT_STRUCTURE_VERSION,
        "semantics_version": MERGE_SEMANTICS_VERSION,
        "status": "completed",
        "config": config.to_dict(),
        "summary": {
            "valid_event_count": len(events),
            "valid_lineage_count": len(lineage_records),
            "valid_policy_count": len(policy_records),
            "unit_count": len(units),
            "job_count": job_count if job_count is not None else len(units) + len(queue_rejections),
            "decision_count": decision_count,
            "hit_detail_count": hit_count,
            "rejection_code_count": len(rejection_items),
            "rejected_record_count": len(rejected_record_keys),
            "read_error_count": len(read_error_items),
            "lineage_issue_count": len(lineage_view.issues),
            "fork_count": len(lineage_view.forks),
            "policy_conflict_count": len(policy_view.conflicts),
            "tombstone_override_count": len(policy_view.tombstone_overrides),
            "queue_rejection_count": len(queue_rejections),
            "timeout_unit_count": timeout_count,
            "budget_unit_count": budget_count,
        },
        "rejections": rejection_items,
        "read_errors": read_error_items,
        "units": units,
        "queue_rejections": queue_rejections,
        "lineage_issues": lineage_view.issues,
        "forks": lineage_view.forks,
        "missing_parent_issues": lineage_view.missing,
        "cycle_issues": lineage_view.cycles,
        "self_reference_issues": lineage_view.self_references,
        "policy_conflicts": policy_view.conflicts,
        "tombstone_overrides": policy_view.tombstone_overrides,
        "policies": [policy_summary(policy) for policy in policy_view.policies],
        "runtime": {
            "concurrency": config.concurrency,
            "unit_timings_ms": dict(sorted(timings.items())),
        },
    }
    if extra:
        report.update(extra)
    report["semantic_digest"] = semantic_digest(report)
    return report


def semantic_report(report: dict[str, Any]) -> dict[str, Any]:
    result = {key: report.get(key) for key in SEMANTIC_REPORT_KEYS}
    result["config"] = report.get("config")
    return result


def semantic_digest(report: dict[str, Any]) -> str:
    return stable_hash(semantic_report(report))


def compare_semantic_reports(
    incremental: dict[str, Any], full: dict[str, Any]
) -> list[dict[str, Any]]:
    differences: list[dict[str, Any]] = []
    for key in (*SEMANTIC_REPORT_KEYS, "config"):
        left = canonical_json(incremental.get(key))
        right = canonical_json(full.get(key))
        if left != right:
            differences.append(
                {
                    "key": key,
                    "incremental_digest": stable_hash(incremental.get(key)),
                    "full_digest": stable_hash(full.get(key)),
                }
            )

    incremental_units = {unit["unit_id"]: unit for unit in incremental.get("units", [])}
    full_units = {unit["unit_id"]: unit for unit in full.get("units", [])}
    for unit_id in sorted(set(incremental_units) | set(full_units)):
        left_unit = incremental_units.get(unit_id)
        right_unit = full_units.get(unit_id)
        if stable_hash(left_unit) != stable_hash(right_unit):
            differences.append(
                {
                    "key": "unit",
                    "unit_id": unit_id,
                    "incremental_digest": stable_hash(left_unit),
                    "full_digest": stable_hash(right_unit),
                }
            )
    return differences
