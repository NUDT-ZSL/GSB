"""Deterministic observatory workload generator.

The generator intentionally models both normal telemetry and a few malformed or
boundary traces.  A fixed seed always produces byte-identical JSON regardless of
the process, locale, current time, or Python's global random state.
"""

from __future__ import annotations

import copy
from typing import Any

from .util import StableRandom, iso_from_us, sha256_json, stable_u64


WORKLOAD_FORMAT = "observatory-telemetry-workload"
WORKLOAD_VERSION = "1.0"
BASE_TIME_US = 1_767_225_600_000_000  # 2026-01-01T00:00:00Z
HOUR_US = 3_600_000_000
DAY_US = 24 * HOUR_US

PROJECT_COUNT = 3
EXPOSURE_COUNT = 5
ARCHIVE_COUNT = 2

SERVICES = (
    "telescope-control",
    "camera-control",
    "camera-readout",
    "data-archive",
    "object-store",
    "catalog",
)


def _ms(value: int) -> int:
    return value * 1_000


def _node(
    index: int,
    parent: int | None,
    service: str,
    operation: str,
    offset_ms: int,
    duration_ms: int,
) -> dict[str, Any]:
    return {
        "index": index,
        "parent_index": parent,
        "service": service,
        "operation": operation,
        "offset_us": _ms(offset_ms),
        "duration_us": _ms(duration_ms),
    }


EXPOSURE_TEMPLATE = (
    _node(0, None, "telescope-control", "StartExposure", 0, 100),
    _node(1, 0, "telescope-control", "PointTelescope", 30, 420),
    _node(2, 0, "camera-control", "ConfigureCamera", 50, 650),
    _node(3, 2, "camera-readout", "ExposeDetector", 120, 1_950),
    _node(4, 3, "camera-readout", "ReadDetector", 250, 620),
    _node(5, 0, "data-archive", "TransferFrame", 90, 980),
    _node(6, 5, "data-archive", "ChecksumFrame", 150, 220),
    _node(7, 5, "object-store", "PersistRawFrame", 300, 560),
    _node(8, 0, "catalog", "RegisterExposure", 220, 150),
)

ARCHIVE_TEMPLATE = (
    _node(0, None, "data-archive", "StartArchiveJob", 0, 100),
    _node(1, 0, "data-archive", "ValidateManifest", 20, 210),
    _node(2, 0, "object-store", "IngestProduct", 80, 820),
    _node(3, 2, "catalog", "IndexMetadata", 180, 230),
    _node(4, 2, "object-store", "VerifyChecksum", 500, 260),
    _node(5, 0, "monitor", "PublishArchiveStatus", 650, 120),
)


def _duration_for(rng: StableRandom, base_ms: int, floor_pct: int = 82, ceil_pct: int = 135) -> int:
    low = max(20, base_ms * floor_pct // 100)
    high = max(low, base_ms * ceil_pct // 100)
    return _ms(rng.randint(low, high))


def _build_nodes(
    template: tuple[dict[str, Any], ...],
    rng: StableRandom,
    *,
    edge_root_duration_us: int | None = None,
) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for spec in template:
        item = {
            "index": spec["index"],
            "parent_index": spec["parent_index"],
            "service": spec["service"],
            "operation": spec["operation"],
            "offset_us": spec["offset_us"],
            "duration_us": _duration_for(rng, spec["duration_us"] // 1_000),
        }
        nodes.append(item)

    # Parents are inclusive: extend an ancestor until it contains every child
    # plus deterministic processing headroom.  This gives realistic nested spans.
    for index in range(len(nodes) - 1, -1, -1):
        parent_index = nodes[index]["parent_index"]
        if parent_index is None:
            continue
        child = nodes[index]
        parent = nodes[parent_index]
        child_end = child["offset_us"] + child["duration_us"]
        needed = max(0, child_end - parent["offset_us"] + _ms(rng.randint(8, 45)))
        parent["duration_us"] = max(parent["duration_us"], needed)

    if edge_root_duration_us is not None:
        # The boundary case makes the root duration exactly equal to the policy
        # threshold.  All descendants still fit inside it.
        nodes[0]["duration_us"] = edge_root_duration_us

    return nodes


def _marker_decision(
    rng: StableRandom,
    project_index: int,
    trace_index: int,
) -> tuple[str, str]:
    forced = {
        (0, 0): ("DROP", "local-override"),
        (0, 1): ("AUTO", "default-rate"),
        (0, 2): ("KEEP", "upstream-collector"),
    }
    if (project_index, trace_index) in forced:
        return forced[(project_index, trace_index)]

    roll = rng.randbelow(100)
    if roll < 18:
        return "KEEP", rng.choice(("upstream-collector", "local-override"))
    if roll < 36:
        return "DROP", rng.choice(("upstream-collector", "local-override"))
    return "AUTO", "default-rate"


def _attributes_for(
    *,
    project_index: int,
    trace_index: int,
    node: dict[str, Any],
    observation_id: str,
    marker: str,
    marker_source: str,
) -> dict[str, Any]:
    attrs: dict[str, Any] = {
        "batch.sequence": project_index * 100 + trace_index,
        "deployment.environment": f"summit-{project_index + 1}",
        "host.name": f"{node['service']}-node-{stable_u64(observation_id, node['service']) % 7 + 1:02d}",
        "instrument": "wide-field-camera",
        "observation.id": observation_id,
        "operation.phase": node["operation"],
        "site.name": "南山观测站",
    }
    if node["parent_index"] is None:
        attrs["sampling.decision"] = marker
        attrs["sampling.marker_source"] = marker_source
    return attrs


def _make_policy(project_index: int, rng: StableRandom) -> dict[str, Any]:
    rates = (4_000, 3_500, 4_500)
    thresholds_ms = (6_000, 4_500, 5_000)
    rate_bps = rates[project_index % len(rates)]
    threshold_ms = thresholds_ms[project_index % len(thresholds_ms)]
    return {
        "head_marker_attribute": "sampling.decision",
        "head_marker_source_attribute": "sampling.marker_source",
        "head_sampling_rate": rate_bps / 10_000,
        "head_sampling_rate_basis_points": rate_bps,
        "retention_overrides_us": {},
        "tail_duration_comparison": "gt",
        "tail_duration_enabled": True,
        "tail_duration_threshold_ms": threshold_ms,
        "tail_error_enabled": True,
        "tail_error_statuses": ["ERROR"],
        "version": "sampling-policy/1",
    }


def _retention_for(trace_index: int, project_index: int) -> tuple[int, str]:
    options = (
        (12 * HOUR_US, "12h"),
        (36 * HOUR_US, "36h"),
        (72 * HOUR_US, "72h"),
        (168 * HOUR_US, "168h"),
    )
    return options[(trace_index + project_index) % len(options)]


def _descendants(template: tuple[dict[str, Any], ...], parent_index: int) -> set[int]:
    result: set[int] = set()
    changed = True
    while changed:
        changed = False
        for spec in template:
            if spec["index"] in result:
                continue
            if spec["parent_index"] == parent_index or spec["parent_index"] in result:
                result.add(spec["index"])
                changed = True
    return result


def _build_trace(
    *,
    seed: int,
    seed_token: str,
    project_index: int,
    trace_index: int,
    job_id: str,
    trace_id: str,
    job_kind: str,
    observation_id: str,
    trace_start_us: int,
    template: tuple[dict[str, Any], ...],
    rng: StableRandom,
    policy: dict[str, Any],
    edge_cases: dict[str, str],
    error_node_index: int | None = None,
    omit_descendants_of: int | None = None,
    omit_only_node: int | None = None,
    remove_parent_field_node: int | None = None,
    self_parent_node: int | None = None,
    duplicate_node: int | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    threshold_us = int(policy["tail_duration_threshold_ms"]) * 1_000
    edge_root_duration = None
    if edge_cases.get("duration_equal_threshold_trace_id") == trace_id:
        edge_root_duration = threshold_us

    nodes = _build_nodes(template, rng, edge_root_duration_us=edge_root_duration)
    marker, marker_source = _marker_decision(rng, project_index, trace_index)

    omitted: set[int] = set()
    if omit_descendants_of is not None:
        omitted |= _descendants(template, omit_descendants_of)
    if omit_only_node is not None:
        omitted.add(omit_only_node)

    definitions: list[dict[str, Any]] = []
    for node in nodes:
        span_id = f"sp-{seed_token}-{project_index + 1:02d}-{trace_index + 1:03d}-{node['index']:02d}"
        parent_span_id = None
        if node["parent_index"] is not None:
            parent_span_id = (
                f"sp-{seed_token}-{project_index + 1:02d}-{trace_index + 1:03d}-{node['parent_index']:02d}"
            )
        status = "OK"
        attrs = _attributes_for(
            project_index=project_index,
            trace_index=trace_index,
            node=node,
            observation_id=observation_id,
            marker=marker,
            marker_source=marker_source,
        )
        if node["index"] == error_node_index:
            status = "ERROR"
            attrs["error.type"] = rng.choice(("ReadoutTimeout", "ArchiveUnavailable", "ChecksumMismatch"))
            attrs["error.message"] = "downstream operation failed"
        definitions.append(
            {
                "attributes": attrs,
                "duration_us": node["duration_us"],
                "operation": node["operation"],
                "parent_span_id": parent_span_id,
                "service": node["service"],
                "span_id": span_id,
                "start_time_us": trace_start_us + node["offset_us"],
                "status": status,
            }
        )

    chain_definition = {
        "ingress": {
            "marker_source": marker_source,
            "sampling_marker": marker,
        },
        "job_id": job_id,
        "job_kind": job_kind,
        "nodes": definitions,
        "observation_id": observation_id,
        "root_span_id": definitions[0]["span_id"],
        "trace_id": trace_id,
    }

    events: list[dict[str, Any]] = []
    node_by_index = {node["index"]: node for node in nodes}
    definition_by_index = {index: definitions[index] for index in range(len(definitions))}
    for index, definition in definition_by_index.items():
        if index in omitted:
            continue
        event = {
            "_sort_dup": 0,
            "attributes": copy.deepcopy(definition["attributes"]),
            "duration_us": definition["duration_us"],
            "event_time_us": definition["start_time_us"] + definition["duration_us"],
            "event_type": "span",
            "job_id": job_id,
            "operation": definition["operation"],
            "parent_span_id": definition["parent_span_id"],
            "service": definition["service"],
            "span_id": definition["span_id"],
            "start_time_us": definition["start_time_us"],
            "status": definition["status"],
            "trace_id": trace_id,
        }
        if index == remove_parent_field_node:
            event.pop("parent_span_id", None)
        if index == self_parent_node:
            event["parent_span_id"] = event["span_id"]
        events.append(event)

        if index == duplicate_node:
            duplicate = copy.deepcopy(event)
            duplicate["_sort_dup"] = 1
            duplicate["event_time_us"] = event["event_time_us"] + 1
            duplicate["attributes"]["telemetry.duplicate"] = True
            events.append(duplicate)

    edge_cases = copy.deepcopy(edge_cases)
    edge_cases["_trace_id"] = trace_id
    return chain_definition, events, edge_cases


def generate_workload(seed: int) -> dict[str, Any]:
    """Build the complete deterministic workload for ``seed``."""
    seed = int(seed)
    seed_token = f"{stable_u64('trace-token', seed) & 0xFFFFFFFF:08x}"
    projects: list[dict[str, Any]] = []

    for project_index in range(PROJECT_COUNT):
        project_rng = StableRandom(seed, f"project-{project_index}")
        project_id = f"obsproj-{project_index + 1:02d}-{seed_token}"
        project_base_us = BASE_TIME_US + (seed % 17) * DAY_US + project_index * 3 * DAY_US
        policy = _make_policy(project_index, project_rng)

        trace_specs: list[dict[str, Any]] = []
        for exposure_index in range(EXPOSURE_COUNT):
            trace_id = f"tr-{seed_token}-{project_index + 1:02d}-e{exposure_index + 1:02d}"
            job_id = f"EXP-{seed_token}-{project_index + 1:02d}-{exposure_index + 1:02d}"
            trace_specs.append(
                {
                    "job_id": job_id,
                    "job_kind": "exposure",
                    "observation_id": f"OBS-{seed_token}-{project_index + 1:02d}-{exposure_index + 1:02d}",
                    "trace_id": trace_id,
                    "trace_start_us": project_base_us + exposure_index * 2 * HOUR_US,
                    "template": EXPOSURE_TEMPLATE,
                }
            )
        for archive_index in range(ARCHIVE_COUNT):
            trace_id = f"tr-{seed_token}-{project_index + 1:02d}-a{archive_index + 1:02d}"
            job_id = f"ARC-{seed_token}-{project_index + 1:02d}-{archive_index + 1:02d}"
            trace_specs.append(
                {
                    "job_id": job_id,
                    "job_kind": "archive",
                    "observation_id": f"OBS-{seed_token}-{project_index + 1:02d}-{EXPOSURE_COUNT + archive_index + 1:02d}",
                    "trace_id": trace_id,
                    "trace_start_us": project_base_us + (EXPOSURE_COUNT + archive_index) * 2 * HOUR_US,
                    "template": ARCHIVE_TEMPLATE,
                }
            )

        edge_cases: dict[str, str] = {}
        if project_index == 0:
            edge_cases["terminal_error_trace_id"] = trace_specs[0]["trace_id"]
            edge_cases["duration_equal_threshold_trace_id"] = trace_specs[1]["trace_id"]
            edge_cases["head_drop_tail_rescue_trace_id"] = trace_specs[0]["trace_id"]
            edge_cases["duplicate_span_trace_id"] = trace_specs[5]["trace_id"]
            edge_cases["missing_parent_trace_id"] = trace_specs[6]["trace_id"]
            edge_cases["duration_equal_threshold_span_id"] = (
                f"sp-{seed_token}-01-02-00"
            )
        elif project_index == 1:
            edge_cases["internal_error_trace_id"] = trace_specs[0]["trace_id"]
            edge_cases["missing_parent_field_trace_id"] = trace_specs[1]["trace_id"]
            edge_cases["self_parent_trace_id"] = trace_specs[2]["trace_id"]

        chain_definitions: list[dict[str, Any]] = []
        all_events: list[dict[str, Any]] = []
        jobs: list[dict[str, Any]] = []

        for trace_index, spec in enumerate(trace_specs):
            trace_rng = StableRandom(seed, f"project-{project_index}-trace-{trace_index}")
            error_node_index = None
            omit_descendants_of = None
            omit_only_node = None
            remove_parent_field_node = None
            self_parent_node = None
            duplicate_node = None

            if project_index == 0 and trace_index == 0:
                error_node_index = 4  # terminal detector-readout span
            elif project_index == 1 and trace_index == 0:
                error_node_index = 2  # internal archive branch, descendants are missing
                omit_descendants_of = 2
            elif project_index == 0 and trace_index == 5:
                duplicate_node = 1
            elif project_index == 0 and trace_index == 6:
                omit_only_node = 2  # children 3 and 4 become parent-missing orphans
            elif project_index == 1 and trace_index == 1:
                remove_parent_field_node = 2
            elif project_index == 1 and trace_index == 2:
                self_parent_node = 2
            elif project_index == 2 and trace_index == 3 and trace_rng.chance(1, 2):
                error_node_index = 5
            elif trace_rng.chance(18, 100):
                candidates = [1, 2, 5, 6] if spec["job_kind"] == "exposure" else [1, 2, 3, 4, 5]
                error_node_index = trace_rng.choice(candidates)
                if error_node_index < 5 and trace_rng.chance(2, 3):
                    omit_descendants_of = error_node_index

            chain_definition, events, annotation = _build_trace(
                seed=seed,
                seed_token=seed_token,
                project_index=project_index,
                trace_index=trace_index,
                job_id=spec["job_id"],
                trace_id=spec["trace_id"],
                job_kind=spec["job_kind"],
                observation_id=spec["observation_id"],
                trace_start_us=spec["trace_start_us"],
                template=spec["template"],
                rng=trace_rng,
                policy=policy,
                edge_cases=edge_cases,
                error_node_index=error_node_index,
                omit_descendants_of=omit_descendants_of,
                omit_only_node=omit_only_node,
                remove_parent_field_node=remove_parent_field_node,
                self_parent_node=self_parent_node,
                duplicate_node=duplicate_node,
            )
            if "_trace_id" in annotation:
                annotation.pop("_trace_id")
            chain_definitions.append(chain_definition)
            all_events.extend(events)

            retention_us, retention_label = _retention_for(trace_index, project_index)
            related = []
            if spec["job_kind"] == "archive":
                related = [item["job_id"] for item in trace_specs[:EXPOSURE_COUNT]]
            jobs.append(
                {
                    "declared_start_us": spec["trace_start_us"],
                    "job_id": spec["job_id"],
                    "job_kind": spec["job_kind"],
                    "observation_id": spec["observation_id"],
                    "related_job_ids": related,
                    "retention_label": retention_label,
                    "retention_us": retention_us,
                    "trace_id": spec["trace_id"],
                }
            )

        # Assign global event sequence after ordering the interleaved stream.
        all_events.sort(
            key=lambda event: (
                event["event_time_us"],
                event["trace_id"],
                event["span_id"],
                event["_sort_dup"],
            )
        )
        normalized_events: list[dict[str, Any]] = []
        for sequence, event in enumerate(all_events):
            event.pop("_sort_dup", None)
            event["event_id"] = f"evt-{seed_token}-{project_index + 1:02d}-{sequence:05d}"
            event["sequence"] = sequence
            normalized_events.append(event)

        projects.append(
            {
                "chain_definitions": chain_definitions,
                "edge_cases": edge_cases,
                "events": normalized_events,
                "jobs": jobs,
                "policy": policy,
                "project_id": project_id,
                "time_origin": {
                    "now_us": project_base_us + 96 * HOUR_US,
                    "window_end_us": project_base_us + 60 * HOUR_US,
                    "window_start_us": project_base_us,
                },
            }
        )

    workload: dict[str, Any] = {
        "format": WORKLOAD_FORMAT,
        "generation": {
            "archive_jobs_per_project": ARCHIVE_COUNT,
            "base_time_us": BASE_TIME_US,
            "exposure_jobs_per_project": EXPOSURE_COUNT,
            "projects": PROJECT_COUNT,
        },
        "projects": projects,
        "seed": seed,
        "seed_token": seed_token,
        "version": WORKLOAD_VERSION,
    }
    workload["content_sha256"] = sha256_json(
        {
            "projects": projects,
            "seed": seed,
            "version": WORKLOAD_VERSION,
        }
    )
    return workload


def workload_summary(workload: dict[str, Any]) -> dict[str, Any]:
    projects = workload.get("projects", [])
    return {
        "archive_jobs": sum(
            1
            for project in projects
            for job in project.get("jobs", [])
            if job.get("job_kind") == "archive"
        ),
        "events": sum(len(project.get("events", [])) for project in projects),
        "exposure_jobs": sum(
            1
            for project in projects
            for job in project.get("jobs", [])
            if job.get("job_kind") == "exposure"
        ),
        "projects": len(projects),
        "time_origin": iso_from_us(BASE_TIME_US),
    }
