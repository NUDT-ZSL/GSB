"""End-to-end evaluation, querying, accounting, and local policy re-evaluation."""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any, Iterable, Iterator

from .generator import WORKLOAD_FORMAT, WORKLOAD_VERSION
from .restore import analyze_failure, restore_trace
from .sampling import (
    apply_policy_patch,
    decide_trace,
    normalize_policy,
    trace_affected_by_policy,
)
from .util import (
    attribute_bytes,
    canonical_clone,
    compact_json,
    deep_copy,
    sha256_json,
    sha256_text,
)


RESULT_FORMAT = "observatory-telemetry-result"
RESULT_VERSION = "1.0"
HOUR_US = 3_600_000_000
DEFAULT_RETENTION_US = 168 * HOUR_US
RESULT_SEMANTICS = {
    "attribute_bytes": "UTF-8 byte length of canonical-JSON(span.attributes)",
    "critical_path": (
        "maximum sum(duration_us) root-to-leaf path over observed real spans; "
        "ties choose the lexicographically smallest span-id sequence"
    ),
    "head_sampling": (
        "KEEP/DROP ingress marker overrides ratio; AUTO uses "
        "first-64-bit SHA-256(seed,project,trace) mod 10000 and bucket < rate"
    ),
    "query_time_filter": "trace_start_us >= from_us and trace_start_us < to_us",
    "retention_expiry": "trace_start_us + retention_us <= as_of_us",
    "tail_sampling": (
        "all traces are inspected; final retention is head OR tail, so tail can "
        "rescue a head-dropped error/slow trace"
    ),
}


class WorkloadError(ValueError):
    """Raised when a loaded workload does not satisfy the input contract."""


def validate_workload(workload: dict[str, Any]) -> None:
    if not isinstance(workload, dict):
        raise WorkloadError("workload must be a JSON object")
    if workload.get("format") not in (None, WORKLOAD_FORMAT):
        raise WorkloadError(f"unexpected workload format: {workload.get('format')!r}")
    if "seed" not in workload:
        raise WorkloadError("workload.seed is required")
    try:
        int(workload["seed"])
    except (TypeError, ValueError) as exc:
        raise WorkloadError("workload.seed must be an integer") from exc
    projects = workload.get("projects")
    if not isinstance(projects, list) or not projects:
        raise WorkloadError("workload.projects must be a non-empty list")
    seen_projects: set[str] = set()
    for project_index, project in enumerate(projects):
        if not isinstance(project, dict):
            raise WorkloadError(f"projects[{project_index}] must be an object")
        project_id = str(project.get("project_id", ""))
        if not project_id:
            raise WorkloadError(f"projects[{project_index}].project_id is required")
        if project_id in seen_projects:
            raise WorkloadError(f"duplicate project_id: {project_id}")
        seen_projects.add(project_id)
        if not isinstance(project.get("policy"), dict):
            raise WorkloadError(f"project {project_id} must declare policy")
        normalize_policy(project.get("policy"))
        if not isinstance(project.get("chain_definitions"), list):
            raise WorkloadError(f"project {project_id} must declare chain_definitions")
        if not isinstance(project.get("events"), list):
            raise WorkloadError(f"project {project_id} must declare events")
        if not isinstance(project.get("jobs"), list):
            raise WorkloadError(f"project {project_id} must declare jobs")


def _event_key(event: dict[str, Any]) -> str:
    value = event.get("trace_id")
    return str(value) if value not in (None, "") else "__unassigned__"


def _definition_start(chain_definition: dict[str, Any] | None) -> int | None:
    if not chain_definition:
        return None
    starts: list[int] = []
    for node in chain_definition.get("nodes", []):
        try:
            starts.append(int(node.get("start_time_us")))
        except (TypeError, ValueError):
            continue
    return min(starts) if starts else None


def _event_start(events: Iterable[dict[str, Any]]) -> int | None:
    starts: list[int] = []
    for event in events:
        try:
            starts.append(int(event.get("start_time_us")))
        except (TypeError, ValueError):
            continue
    return min(starts) if starts else None


def _project_context(
    project: dict[str, Any],
    project_index: int,
    seed: int,
) -> dict[str, Any]:
    project_id = str(project["project_id"])
    policy = normalize_policy(project.get("policy"))
    diagnostics: list[dict[str, Any]] = []
    jobs_by_id: dict[str, dict[str, Any]] = {}
    for job in project.get("jobs", []):
        job_id = str(job.get("job_id", ""))
        if not job_id:
            diagnostics.append(
                {
                    "code": "JOB_ID_MISSING",
                    "details": {"project_index": project_index},
                    "related_id": None,
                    "severity": "error",
                    "span_id": None,
                    "trace_id": None,
                }
            )
            continue
        if job_id in jobs_by_id:
            diagnostics.append(
                {
                    "code": "DUPLICATE_JOB_DECLARATION",
                    "details": {"job_id": job_id},
                    "related_id": job_id,
                    "severity": "warning",
                    "span_id": None,
                    "trace_id": None,
                }
            )
            continue
        jobs_by_id[job_id] = job

    definitions_by_trace: dict[str, dict[str, Any]] = {}
    for definition in project.get("chain_definitions", []):
        trace_id = str(definition.get("trace_id", ""))
        if not trace_id:
            diagnostics.append(
                {
                    "code": "CHAIN_TRACE_ID_MISSING",
                    "details": {"project_id": project_id},
                    "related_id": None,
                    "severity": "error",
                    "span_id": None,
                    "trace_id": None,
                }
            )
            continue
        if trace_id in definitions_by_trace:
            diagnostics.append(
                {
                    "code": "DUPLICATE_CHAIN_DEFINITION",
                    "details": {
                        "chosen": "first_in_input_order",
                        "trace_id": trace_id,
                    },
                    "related_id": trace_id,
                    "severity": "warning",
                    "span_id": None,
                    "trace_id": trace_id,
                }
            )
            continue
        definitions_by_trace[trace_id] = definition

    events_by_trace: dict[str, list[dict[str, Any]]] = defaultdict(list)
    unassigned_count = 0
    for event in project.get("events", []):
        key = _event_key(event)
        if key == "__unassigned__":
            unassigned_count += 1
        events_by_trace[key].append(event)
    if unassigned_count:
        diagnostics.append(
            {
                "code": "TRACE_ID_MISSING",
                "details": {"event_count": unassigned_count},
                "related_id": "__unassigned__",
                "severity": "error",
                "span_id": None,
                "trace_id": "__unassigned__",
            }
        )

    all_trace_ids = sorted(set(definitions_by_trace) | set(events_by_trace))
    descriptors: list[dict[str, Any]] = []
    for trace_id in all_trace_ids:
        definition = definitions_by_trace.get(trace_id)
        events = events_by_trace.get(trace_id, [])
        definition_job_id = (
            str(definition.get("job_id"))
            if definition and definition.get("job_id") not in (None, "")
            else None
        )
        event_job_ids = sorted(
            {
                str(event.get("job_id"))
                for event in events
                if event.get("job_id") not in (None, "")
            }
        )
        job_id = definition_job_id or (event_job_ids[0] if event_job_ids else "__unassigned__")
        job = jobs_by_id.get(job_id, {})
        start_candidates = [
            value
            for value in (
                _event_start(events),
                _definition_start(definition),
                int(job["declared_start_us"]) if "declared_start_us" in job else None,
            )
            if value is not None
        ]
        start_us = min(start_candidates) if start_candidates else 0
        descriptors.append(
            {
                "chain_definition": definition,
                "events": events,
                "job": job,
                "job_id": job_id,
                "start_us": start_us,
                "trace_id": trace_id,
            }
        )
    descriptors.sort(key=lambda item: (item["start_us"], item["trace_id"]))
    for ordinal, descriptor in enumerate(descriptors, start=1):
        descriptor["ratio_ordinal"] = ordinal

    return {
        "descriptors": descriptors,
        "diagnostics": diagnostics,
        "jobs_by_id": jobs_by_id,
        "policy": policy,
        "project": project,
        "project_id": project_id,
        "seed": seed,
    }


def prepare_contexts(workload: dict[str, Any]) -> list[dict[str, Any]]:
    validate_workload(workload)
    seed = int(workload["seed"])
    return [
        _project_context(project, index, seed)
        for index, project in enumerate(workload.get("projects", []))
    ]


def _retention_for_trace(
    context: dict[str, Any],
    job_id: str,
) -> dict[str, Any]:
    job = context["jobs_by_id"].get(job_id, {})
    policy = context["policy"]
    if job_id in policy.get("retention_overrides_us", {}):
        retention_us = int(policy["retention_overrides_us"][job_id])
        source = "policy_override"
    elif "retention_us" in job:
        retention_us = int(job["retention_us"])
        source = "job_declaration"
    else:
        retention_us = DEFAULT_RETENTION_US
        source = "default"
    declared_start = job.get("declared_start_us")
    return {
        "declared_start_us": int(declared_start) if declared_start is not None else None,
        "retention_source": source,
        "retention_us": retention_us,
    }


def build_trace_object(context: dict[str, Any], descriptor: dict[str, Any]) -> dict[str, Any]:
    restoration = restore_trace(
        project_id=context["project_id"],
        trace_id=descriptor["trace_id"],
        events=descriptor["events"],
        chain_definition=descriptor["chain_definition"],
        fallback_job_id=descriptor["job_id"],
    )
    job_id = restoration["job_id"]
    decision = decide_trace(
        restoration,
        policy=context["policy"],
        seed=context["seed"],
        project_id=context["project_id"],
        ratio_ordinal=descriptor["ratio_ordinal"],
    )
    failure = analyze_failure(restoration, descriptor["chain_definition"])
    retention = _retention_for_trace(context, job_id)
    job = context["jobs_by_id"].get(job_id, {})
    job_kind = restoration.get("job_kind") or job.get("job_kind") or "unknown"
    observation_id = (
        restoration.get("observation_id")
        or job.get("observation_id")
    )
    total_attribute_bytes = sum(
        span.get("attribute_bytes", attribute_bytes(span.get("attributes", {})))
        for span in restoration["flat_spans"]
    )
    return {
        "attribute_bytes": total_attribute_bytes,
        "failure_analysis": failure,
        "job_declared_start_us": retention["declared_start_us"],
        "job_id": job_id,
        "job_kind": job_kind,
        "observation_id": observation_id,
        "project_id": context["project_id"],
        "restoration": restoration,
        "retained": bool(decision["final"]["retained"]),
        "retention_source": retention["retention_source"],
        "retention_us": retention["retention_us"],
        "sample_decision": decision,
        "span_count": restoration["span_count"],
        "trace_id": descriptor["trace_id"],
        "trace_start_us": restoration["trace_start_us"],
    }


def iter_trace_units(workload: dict[str, Any]) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """Yield contexts and trace descriptors without materializing all trees."""
    for context in prepare_contexts(workload):
        for descriptor in context["descriptors"]:
            yield context, descriptor


def _effective_jobs(context: dict[str, Any]) -> list[dict[str, Any]]:
    project_now_us = int(
        context["project"].get("time_origin", {}).get("now_us", 0)
    )
    result: list[dict[str, Any]] = []
    for job_id in sorted(context["jobs_by_id"]):
        job = copy.deepcopy(context["jobs_by_id"][job_id])
        retention = _retention_for_trace(context, job_id)
        job["effective_retention_us"] = retention["retention_us"]
        job["retention_source"] = retention["retention_source"]
        job["expires_at_us"] = (
            int(job.get("declared_start_us", 0)) + retention["retention_us"]
        )
        job["project_id"] = context["project_id"]
        job["as_of_us"] = project_now_us
        result.append(job)
    return result


def _summarize_project(traces: list[dict[str, Any]]) -> dict[str, Any]:
    retained = [trace for trace in traces if trace["retained"]]
    failed = [trace for trace in traces if trace["failure_analysis"]["failed"]]
    head_kept = [
        trace for trace in traces if trace["sample_decision"]["head"]["kept"]
    ]
    tail_kept = [
        trace for trace in traces if trace["sample_decision"]["tail"]["kept"]
    ]
    tail_rescued = [
        trace for trace in traces if trace["sample_decision"]["final"]["tail_rescued"]
    ]
    return {
        "attribute_bytes_all": sum(trace["attribute_bytes"] for trace in traces),
        "attribute_bytes_retained": sum(trace["attribute_bytes"] for trace in retained),
        "dropped_trace_count": len(traces) - len(retained),
        "failed_retained_trace_count": sum(
            1 for trace in failed if trace["retained"]
        ),
        "failed_trace_count": len(failed),
        "head_kept_trace_count": len(head_kept),
        "retained_span_count": sum(trace["span_count"] for trace in retained),
        "retained_trace_count": len(retained),
        "span_count": sum(trace["span_count"] for trace in traces),
        "tail_kept_trace_count": len(tail_kept),
        "tail_rescued_trace_count": len(tail_rescued),
        "trace_count": len(traces),
    }


def assemble_result(
    workload: dict[str, Any],
    contexts: list[dict[str, Any]],
    traces_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    projects: list[dict[str, Any]] = []
    for context in contexts:
        trace_ids = [descriptor["trace_id"] for descriptor in context["descriptors"]]
        missing = [trace_id for trace_id in trace_ids if trace_id not in traces_by_id]
        if missing:
            raise WorkloadError(f"missing evaluated traces: {missing}")
        traces = [traces_by_id[trace_id] for trace_id in trace_ids]
        diagnostics = list(context["diagnostics"])
        for trace in traces:
            for diagnostic in trace["restoration"]["diagnostics"]:
                diagnostics.append(diagnostic)
        diagnostics.sort(
            key=lambda item: (
                item.get("trace_id") or "",
                item.get("code") or "",
                item.get("span_id") or "",
                compact_json(item.get("details", {})),
            )
        )
        project = context["project"]
        projects.append(
            {
                "diagnostics": diagnostics,
                "edge_cases": copy.deepcopy(project.get("edge_cases", {})),
                "jobs": _effective_jobs(context),
                "policy": canonical_clone(context["policy"]),
                "project_id": context["project_id"],
                "summary": _summarize_project(traces),
                "time_origin": copy.deepcopy(project.get("time_origin", {})),
                "traces": traces,
            }
        )

    project_payload = {
        "projects": projects,
        "seed": int(workload["seed"]),
        "version": RESULT_VERSION,
    }
    return {
        "format": RESULT_FORMAT,
        "projects": projects,
        "result_sha256": sha256_json(project_payload),
        "semantics": canonical_clone(RESULT_SEMANTICS),
        "seed": int(workload["seed"]),
        "version": RESULT_VERSION,
        "workload_format": WORKLOAD_FORMAT,
        "workload_sha256": sha256_json(
            {
                "projects": workload.get("projects", []),
                "seed": int(workload["seed"]),
                "version": workload.get("version", WORKLOAD_VERSION),
            }
        ),
    }


def evaluate_workload(workload: dict[str, Any]) -> dict[str, Any]:
    contexts = prepare_contexts(workload)
    traces_by_id: dict[str, dict[str, Any]] = {}
    for context, descriptor in iter_trace_units(workload):
        traces_by_id[descriptor["trace_id"]] = build_trace_object(context, descriptor)
    return assemble_result(workload, contexts, traces_by_id)


def _trace_matches(
    trace: dict[str, Any],
    *,
    trace_ids: set[str] | None,
    job_ids: set[str] | None,
    project_ids: set[str] | None,
    from_us: int | None,
    to_us: int | None,
) -> bool:
    if trace_ids is not None and trace["trace_id"] not in trace_ids:
        return False
    if job_ids is not None and trace["job_id"] not in job_ids:
        return False
    if project_ids is not None and trace["project_id"] not in project_ids:
        return False
    start = int(trace["trace_start_us"])
    if from_us is not None and start < from_us:
        return False
    if to_us is not None and start >= to_us:
        return False
    return True


def query_result(
    result: dict[str, Any],
    *,
    trace_ids: Iterable[str] | None = None,
    job_ids: Iterable[str] | None = None,
    project_ids: Iterable[str] | None = None,
    from_us: int | None = None,
    to_us: int | None = None,
    include_dropped: bool = False,
) -> dict[str, Any]:
    trace_filter = {str(value) for value in trace_ids} if trace_ids else None
    job_filter = {str(value) for value in job_ids} if job_ids else None
    project_filter = {str(value) for value in project_ids} if project_ids else None
    if from_us is not None and to_us is not None and from_us >= to_us:
        raise ValueError("query start must be before query end")

    matched: list[dict[str, Any]] = []
    for project in result.get("projects", []):
        for trace in project.get("traces", []):
            if _trace_matches(
                trace,
                trace_ids=trace_filter,
                job_ids=job_filter,
                project_ids=project_filter,
                from_us=from_us,
                to_us=to_us,
            ):
                matched.append(trace)
    matched.sort(
        key=lambda trace: (
            trace["job_id"],
            trace["trace_start_us"],
            trace["trace_id"],
        )
    )
    selected = matched if include_dropped else [
        trace for trace in matched if trace["retained"]
    ]

    failures: list[dict[str, Any]] = []
    for trace in selected:
        if not trace["failure_analysis"]["failed"]:
            continue
        failures.append(
            {
                "critical_path": copy.deepcopy(
                    trace["failure_analysis"]["critical_path"]
                ),
                "downstream_missing_nodes": copy.deepcopy(
                    trace["failure_analysis"]["downstream_missing_nodes"]
                ),
                "job_id": trace["job_id"],
                "job_kind": trace["job_kind"],
                "observation_id": trace["observation_id"],
                "project_id": trace["project_id"],
                "root_cause": copy.deepcopy(trace["failure_analysis"]["root_cause"]),
                "trace_id": trace["trace_id"],
                "trace_start_us": trace["trace_start_us"],
            }
        )
    failures.sort(
        key=lambda failure: (
            failure["job_id"],
            failure["trace_start_us"],
            failure["trace_id"],
        )
    )

    by_job: dict[tuple[str, str], dict[str, Any]] = {}
    for trace in selected:
        key = (trace["project_id"], trace["job_id"])
        bucket = by_job.setdefault(
            key,
            {
                "attribute_bytes": 0,
                "failed_trace_count": 0,
                "job_id": trace["job_id"],
                "project_id": trace["project_id"],
                "span_count": 0,
                "trace_count": 0,
                "trace_ids": [],
            },
        )
        bucket["attribute_bytes"] += trace["attribute_bytes"]
        bucket["span_count"] += trace["span_count"]
        bucket["trace_count"] += 1
        if trace["failure_analysis"]["failed"]:
            bucket["failed_trace_count"] += 1
        bucket["trace_ids"].append(trace["trace_id"])
    jobs = sorted(by_job.values(), key=lambda item: (item["project_id"], item["job_id"]))

    return {
        "definitions": canonical_clone(RESULT_SEMANTICS),
        "failures": failures,
        "filters": {
            "from_us": from_us,
            "include_dropped": include_dropped,
            "job_ids": sorted(job_filter) if job_filter else None,
            "project_ids": sorted(project_filter) if project_filter else None,
            "to_us": to_us,
            "trace_ids": sorted(trace_filter) if trace_filter else None,
        },
        "format": "observatory-trace-query",
        "jobs": jobs,
        "matched_but_filtered_trace_count": len(matched) - len(selected),
        "matched_trace_count": len(matched),
        "retained_trace_ids": [
            trace["trace_id"] for trace in selected if trace["retained"]
        ],
        "seed": result.get("seed"),
        "summary": {
            "attribute_bytes": sum(job["attribute_bytes"] for job in jobs),
            "failed_trace_count": len(failures),
            "job_count": len(jobs),
            "span_count": sum(job["span_count"] for job in jobs),
            "trace_count": len(selected),
        },
        "traces": [
            {
                "critical_path": copy.deepcopy(trace["restoration"]["critical_path"]),
                "diagnostics": copy.deepcopy(trace["restoration"]["diagnostics"]),
                "expected_missing_nodes": copy.deepcopy(
                    trace["restoration"]["expected_missing_nodes"]
                ),
                "failure_analysis": copy.deepcopy(trace["failure_analysis"]),
                "job_id": trace["job_id"],
                "job_kind": trace["job_kind"],
                "project_id": trace["project_id"],
                "retained": trace["retained"],
                "sample_decision": copy.deepcopy(trace["sample_decision"]),
                "span_count": trace["span_count"],
                "trace_id": trace["trace_id"],
                "trace_start_us": trace["trace_start_us"],
                "tree": copy.deepcopy(trace["restoration"]["tree"]),
            }
            for trace in selected
        ],
        "version": "1.0",
    }


def build_ledger(
    result: dict[str, Any],
    *,
    job_ids: Iterable[str] | None = None,
    project_ids: Iterable[str] | None = None,
    from_us: int | None = None,
    to_us: int | None = None,
    now_us_override: int | None = None,
) -> dict[str, Any]:
    job_filter = {str(value) for value in job_ids} if job_ids else None
    project_filter = {str(value) for value in project_ids} if project_ids else None
    if from_us is not None and to_us is not None and from_us >= to_us:
        raise ValueError("ledger start must be before ledger end")

    trace_details: list[dict[str, Any]] = []
    jobs_map: dict[tuple[str, str], dict[str, Any]] = {}
    for project in result.get("projects", []):
        project_id = project["project_id"]
        if project_filter is not None and project_id not in project_filter:
            continue
        project_now = (
            int(now_us_override)
            if now_us_override is not None
            else int(project.get("time_origin", {}).get("now_us", 0))
        )
        jobs_by_id = {job["job_id"]: job for job in project.get("jobs", [])}
        for trace in project.get("traces", []):
            if job_filter is not None and trace["job_id"] not in job_filter:
                continue
            if not _trace_matches(
                trace,
                trace_ids=None,
                job_ids=None,
                project_ids=None,
                from_us=from_us,
                to_us=to_us,
            ):
                continue
            retention_us = int(trace["retention_us"])
            expires_at_us = int(trace["trace_start_us"]) + retention_us
            expired = bool(trace["retained"] and expires_at_us <= project_now)
            retained = bool(trace["retained"])
            detail = {
                "attribute_bytes": trace["attribute_bytes"] if retained else 0,
                "decision": trace["sample_decision"]["final"]["decision"],
                "expired": expired,
                "expires_at_us": expires_at_us,
                "job_id": trace["job_id"],
                "project_id": project_id,
                "retained": retained,
                "retention_us": retention_us,
                "span_count": trace["span_count"] if retained else 0,
                "trace_id": trace["trace_id"],
                "trace_start_us": trace["trace_start_us"],
            }
            trace_details.append(detail)
            key = (project_id, trace["job_id"])
            job = jobs_by_id.get(trace["job_id"], {})
            bucket = jobs_map.setdefault(
                key,
                {
                    "attribute_bytes": 0,
                    "declared_retention_us": int(
                        job.get("effective_retention_us", retention_us)
                    ),
                    "dropped_trace_count": 0,
                    "expired_attribute_bytes": 0,
                    "expired_span_count": 0,
                    "expired_trace_count": 0,
                    "job_id": trace["job_id"],
                    "job_kind": trace["job_kind"],
                    "now_us": project_now,
                    "over_retention": False,
                    "over_retention_trace_ids": [],
                    "project_id": project_id,
                    "retained_span_count": 0,
                    "retained_trace_count": 0,
                    "retention_us": retention_us,
                    "span_count": 0,
                    "trace_count": 0,
                    "trace_ids": [],
                },
            )
            bucket["trace_count"] += 1
            bucket["trace_ids"].append(trace["trace_id"])
            if retained:
                bucket["retained_trace_count"] += 1
                bucket["retained_span_count"] += int(trace["span_count"])
                bucket["span_count"] += int(trace["span_count"])
                bucket["attribute_bytes"] += int(trace["attribute_bytes"])
                if expired:
                    bucket["expired_trace_count"] += 1
                    bucket["expired_span_count"] += int(trace["span_count"])
                    bucket["expired_attribute_bytes"] += int(trace["attribute_bytes"])
                    bucket["over_retention"] = True
                    bucket["over_retention_trace_ids"].append(trace["trace_id"])
            else:
                bucket["dropped_trace_count"] += 1

    trace_details.sort(
        key=lambda detail: (
            detail["project_id"],
            detail["job_id"],
            detail["trace_start_us"],
            detail["trace_id"],
        )
    )
    jobs = sorted(jobs_map.values(), key=lambda item: (item["project_id"], item["job_id"]))
    for job in jobs:
        job["over_retention_trace_ids"].sort()
        job["trace_ids"].sort()

    totals = {
        "attribute_bytes": sum(job["attribute_bytes"] for job in jobs),
        "dropped_trace_count": sum(job["dropped_trace_count"] for job in jobs),
        "expired_attribute_bytes": sum(job["expired_attribute_bytes"] for job in jobs),
        "expired_span_count": sum(job["expired_span_count"] for job in jobs),
        "expired_trace_count": sum(job["expired_trace_count"] for job in jobs),
        "job_count": len(jobs),
        "retained_span_count": sum(job["retained_span_count"] for job in jobs),
        "retained_trace_count": sum(job["retained_trace_count"] for job in jobs),
        "span_count": sum(job["span_count"] for job in jobs),
        "trace_count": sum(job["trace_count"] for job in jobs),
    }
    decision_aggregate = {
        "attribute_bytes": sum(
            detail["attribute_bytes"] for detail in trace_details
        ),
        "retained_span_count": sum(
            detail["span_count"] for detail in trace_details
        ),
        "retained_trace_count": sum(
            1 for detail in trace_details if detail["retained"]
        ),
    }
    reconciliation = {
        "attribute_bytes_equal": totals["attribute_bytes"]
        == decision_aggregate["attribute_bytes"],
        "retained_span_count_equal": totals["retained_span_count"]
        == decision_aggregate["retained_span_count"],
        "retained_trace_count_equal": totals["retained_trace_count"]
        == decision_aggregate["retained_trace_count"],
        "source": "per_trace_sampling_decisions",
    }
    reconciliation["all_equal"] = all(
        value
        for key, value in reconciliation.items()
        if key.endswith("_equal")
    )

    return {
        "definitions": {
            "attribute_bytes": RESULT_SEMANTICS["attribute_bytes"],
            "retention_expiry": RESULT_SEMANTICS["retention_expiry"],
            "time_filter": RESULT_SEMANTICS["query_time_filter"],
            "trace_accounting": (
                "one retained trace contributes its restored real spans once; "
                "duplicate telemetry events are never counted twice"
            ),
        },
        "filters": {
            "from_us": from_us,
            "job_ids": sorted(job_filter) if job_filter else None,
            "now_us_override": now_us_override,
            "project_ids": sorted(project_filter) if project_filter else None,
            "to_us": to_us,
        },
        "format": "observatory-retention-ledger",
        "jobs": jobs,
        "reconciliation": reconciliation,
        "seed": result.get("seed"),
        "totals": totals,
        "trace_decisions": trace_details,
        "version": "1.0",
    }


def _ledger_diff(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(
        {
            (job["project_id"], job["job_id"])
            for job in before["jobs"] + after["jobs"]
        }
    )
    before_map = {
        (job["project_id"], job["job_id"]): job for job in before["jobs"]
    }
    after_map = {
        (job["project_id"], job["job_id"]): job for job in after["jobs"]
    }
    metric_names = (
        "retained_trace_count",
        "retained_span_count",
        "attribute_bytes",
        "expired_trace_count",
        "expired_span_count",
        "expired_attribute_bytes",
    )
    jobs: list[dict[str, Any]] = []
    for key in keys:
        old_job = before_map.get(key, {})
        new_job = after_map.get(key, {})
        before_values = {name: int(old_job.get(name, 0)) for name in metric_names}
        after_values = {name: int(new_job.get(name, 0)) for name in metric_names}
        delta = {
            name: after_values[name] - before_values[name] for name in metric_names
        }
        if any(delta.values()) or bool(old_job.get("over_retention")) != bool(
            new_job.get("over_retention")
        ):
            jobs.append(
                {
                    "after": after_values,
                    "before": before_values,
                    "delta": delta,
                    "job_id": key[1],
                    "over_retention_after": bool(new_job.get("over_retention")),
                    "over_retention_before": bool(old_job.get("over_retention")),
                    "project_id": key[0],
                }
            )
    totals_before = {
        name: int(before["totals"].get(name, 0)) for name in metric_names
    }
    totals_after = {
        name: int(after["totals"].get(name, 0)) for name in metric_names
    }
    return {
        "changed_job_count": len(jobs),
        "jobs": jobs,
        "totals": {
            "after": totals_after,
            "before": totals_before,
            "delta": {
                name: totals_after[name] - totals_before[name]
                for name in metric_names
            },
        },
    }


def _trace_projection(trace: dict[str, Any]) -> dict[str, Any]:
    return {
        "failure_analysis": trace["failure_analysis"],
        "job_id": trace["job_id"],
        "job_kind": trace["job_kind"],
        "observation_id": trace["observation_id"],
        "restoration": trace["restoration"],
        "retained": trace["retained"],
        "retention_source": trace["retention_source"],
        "retention_us": trace["retention_us"],
        "sample_decision": trace["sample_decision"],
        "span_count": trace["span_count"],
        "trace_id": trace["trace_id"],
        "trace_start_us": trace["trace_start_us"],
    }


def _compare_results(
    local_result: dict[str, Any],
    full_result: dict[str, Any],
) -> dict[str, Any]:
    local_traces = {
        trace["trace_id"]: _trace_projection(trace)
        for project in local_result["projects"]
        for trace in project["traces"]
    }
    full_traces = {
        trace["trace_id"]: _trace_projection(trace)
        for project in full_result["projects"]
        for trace in project["traces"]
    }
    mismatches: list[dict[str, Any]] = []
    for trace_id in sorted(set(local_traces) | set(full_traces)):
        if compact_json(local_traces.get(trace_id)) != compact_json(full_traces.get(trace_id)):
            mismatches.append(
                {
                    "kind": "trace",
                    "trace_id": trace_id,
                }
            )
    local_without_hash = {
        key: value for key, value in local_result.items() if key != "result_sha256"
    }
    full_without_hash = {
        key: value for key, value in full_result.items() if key != "result_sha256"
    }
    if compact_json(local_without_hash) != compact_json(full_without_hash):
        mismatches.append({"kind": "aggregate_result"})
    return {
        "full_result_sha256": full_result["result_sha256"],
        "local_equals_full": not mismatches,
        "local_result_sha256": local_result["result_sha256"],
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }


def recheck_policy(
    base_workload: dict[str, Any],
    patch: dict[str, Any],
    *,
    base_result: dict[str, Any] | None = None,
    base_ledger: dict[str, Any] | None = None,
    job_ids: Iterable[str] | None = None,
    project_ids: Iterable[str] | None = None,
    from_us: int | None = None,
    to_us: int | None = None,
    now_us_override: int | None = None,
) -> dict[str, Any]:
    if base_result is None:
        base_result = evaluate_workload(base_workload)
    adjusted_workload = apply_policy_patch(base_workload, patch)
    full_result = evaluate_workload(adjusted_workload)
    adjusted_contexts = prepare_contexts(adjusted_workload)
    base_traces = {
        trace["trace_id"]: trace
        for project in base_result["projects"]
        for trace in project["traces"]
    }
    old_policies = {
        project["project_id"]: project["policy"]
        for project in base_workload["projects"]
    }
    local_traces: dict[str, dict[str, Any]] = {}
    evaluated_ids: list[str] = []
    reused_ids: list[str] = []
    retention_updated_ids: list[str] = []
    change_categories: dict[str, list[str]] = {}

    for context in adjusted_contexts:
        old_policy = old_policies[context["project_id"]]
        for descriptor in context["descriptors"]:
            trace_id = descriptor["trace_id"]
            base_trace = base_traces[trace_id]
            affected, categories = trace_affected_by_policy(
                base_trace["restoration"],
                old_policy,
                context["policy"],
            )
            new_retention = _retention_for_trace(context, base_trace["job_id"])
            if affected:
                trace = build_trace_object(context, descriptor)
                evaluated_ids.append(trace_id)
                change_categories[trace_id] = categories
            else:
                trace = deep_copy(base_trace)
                trace["retention_source"] = new_retention["retention_source"]
                trace["retention_us"] = new_retention["retention_us"]
                trace["job_declared_start_us"] = new_retention["declared_start_us"]
                reused_ids.append(trace_id)
                if (
                    trace["retention_us"] != base_trace["retention_us"]
                    or trace["retention_source"] != base_trace["retention_source"]
                ):
                    retention_updated_ids.append(trace_id)
            local_traces[trace_id] = trace

    local_result = assemble_result(adjusted_workload, adjusted_contexts, local_traces)
    changed_decision_ids: list[str] = []
    unchanged_decision_ids: list[str] = []
    for trace_id in sorted(base_traces):
        if compact_json(base_traces[trace_id]["sample_decision"]) != compact_json(
            local_traces[trace_id]["sample_decision"]
        ):
            changed_decision_ids.append(trace_id)
        else:
            unchanged_decision_ids.append(trace_id)
    if base_ledger is None:
        base_ledger = build_ledger(
            base_result,
            job_ids=job_ids,
            project_ids=project_ids,
            from_us=from_us,
            to_us=to_us,
            now_us_override=now_us_override,
        )
    adjusted_ledger = build_ledger(
        local_result,
        job_ids=job_ids,
        project_ids=project_ids,
        from_us=from_us,
        to_us=to_us,
        now_us_override=now_us_override,
    )
    full_ledger = build_ledger(
        full_result,
        job_ids=job_ids,
        project_ids=project_ids,
        from_us=from_us,
        to_us=to_us,
        now_us_override=now_us_override,
    )
    consistency = _compare_results(local_result, full_result)
    consistency["ledger_equal"] = (
        compact_json(adjusted_ledger) == compact_json(full_ledger)
    )
    consistency["all_equal"] = bool(
        consistency["local_equals_full"] and consistency["ledger_equal"]
    )
    return {
        "adjusted_result": local_result,
        "after_ledger": adjusted_ledger,
        "before_ledger": base_ledger,
        "change_categories": dict(sorted(change_categories.items())),
        "changed_decision_trace_count": len(changed_decision_ids),
        "changed_decision_trace_ids": changed_decision_ids,
        "consistency": consistency,
        "evaluated_trace_count": len(evaluated_ids),
        "evaluated_trace_ids": sorted(evaluated_ids),
        "format": "observatory-policy-recheck",
        "ledger_diff": _ledger_diff(base_ledger, adjusted_ledger),
        "patch": canonical_clone(patch),
        "reused_trace_count": len(reused_ids),
        "reused_trace_ids": sorted(reused_ids),
        "retention_updated_trace_count": len(retention_updated_ids),
        "retention_updated_trace_ids": sorted(retention_updated_ids),
        "seed": int(base_workload["seed"]),
        "unchanged_decision_trace_count": len(unchanged_decision_ids),
        "unchanged_decision_trace_ids": unchanged_decision_ids,
        "version": "1.0",
    }
