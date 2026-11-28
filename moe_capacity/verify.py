"""End-to-end acceptance checks for generated or supplied schema triples."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .model import NormalizedProject
from .normalize import (
    NormalizationError,
    canonical_fingerprint,
    normalize_document,
    normalize_file,
)
from .reference import compare_with_reference, run_reference
from .router import run_allocation


def _canonical_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _first_route_difference(
    main: dict[str, Any], other: dict[str, Any]
) -> dict[str, Any] | None:
    left = {row[0]: row[1:] for row in main["assignment_signature"]}
    right = {row[0]: row[1:] for row in other["assignment_signature"]}
    for request_id in sorted(set(left) | set(right)):
        if left.get(request_id) != right.get(request_id):
            return {
                "main": left.get(request_id),
                "other": right.get(request_id),
                "request_id": request_id,
            }
    return None


def _event_signature(result: dict[str, Any]) -> list[Any]:
    return [
        result.get("event_id"),
        result.get("kind"),
        result.get("affected_request_ids"),
        result.get("dependent_expert_ids"),
        result.get("changed_request_ids"),
        result.get("shortfall_bytes"),
        result.get("fallback_used"),
        result.get("feasible_after_event"),
    ]


def _ledger_aggregation_checks(
    project: NormalizedProject,
    result: dict[str, Any],
) -> dict[str, Any]:
    assignments = {
        row["request_id"]: row for row in result["assignments"]
    }
    ledger = result["after_rebalance"]["ledger"]
    errors: list[str] = []
    total_compute = 0
    total_billed = 0
    total_weighted = 0
    for replica in ledger["replicas"]:
        compute = sum(item["compute_load_bytes"] for item in replica["experts"])
        billed = sum(
            item["billed_communication_bytes"] for item in replica["experts"]
        )
        weighted = sum(item["weighted_load_bytes"] for item in replica["experts"])
        samples = sum(item["sample_units"] for item in replica["experts"])
        request_ids = sorted(
            request_id
            for item in replica["experts"]
            for request_id in item["request_ids"]
        )
        expected_ids = sorted(
            request_id
            for request_id, row in assignments.items()
            if row["target_replica"] == replica["replica_id"]
        )
        if compute != replica["compute_load_bytes"]:
            errors.append(f"{replica['replica_id']}: expert compute sum mismatch")
        if billed != replica["billed_communication_bytes"]:
            errors.append(f"{replica['replica_id']}: expert communication sum mismatch")
        if weighted != replica["weighted_load_bytes"]:
            errors.append(f"{replica['replica_id']}: expert weighted sum mismatch")
        if request_ids != expected_ids:
            errors.append(f"{replica['replica_id']}: request membership mismatch")
        for item in replica["experts"]:
            if item["weighted_load_bytes"] != (
                item["compute_load_bytes"] + item["billed_communication_bytes"]
            ):
                errors.append(
                    f"{replica['replica_id']}/{item['expert_id']}: weighted ledger mismatch"
                )
            if item["sample_units"] <= 0:
                errors.append(
                    f"{replica['replica_id']}/{item['expert_id']}: no sample load"
                )
        total_compute += compute
        total_billed += billed
        total_weighted += weighted
    if total_compute != ledger["total_compute_load_bytes"]:
        errors.append("total compute mismatch")
    if total_billed != ledger["total_billed_communication_bytes"]:
        errors.append("total communication mismatch")
    if total_weighted != ledger["total_weighted_load_bytes"]:
        errors.append("total weighted load mismatch")
    if sum(row["sample_units"] for row in assignments.values()) != sum(
        request.sample_units for request in project.requests
    ):
        errors.append("assigned sample total differs from request sample total")
    return {"ok": not errors, "errors": errors}


def _trace_complete(result: dict[str, Any]) -> bool:
    traces = result.get("request_traces", [])
    return len(traces) == len(result.get("assignments", [])) and all(
        trace.get("candidate_experts")
        and trace.get("arrival_selection")
        and trace.get("selected")
        and trace.get("candidate_filter")
        and trace.get("target_selection_basis")
        and trace.get("final_options_by_expert")
        for trace in traces
    )


def _final_capacity_ok(result: dict[str, Any]) -> bool:
    return all(
        row["weighted_load_bytes"] <= row["capacity_bytes"]
        for row in result["after_rebalance"]["ledger"]["replicas"]
    )


def _rebalance_non_worse(result: dict[str, Any]) -> bool:
    before = result["before_rebalance"]["metrics"]["max_replica_load_bytes"]
    after = result["after_rebalance"]["metrics"]["max_replica_load_bytes"]
    return after <= before


def _normalize_source(
    version: str,
    source: str | Path | dict[str, Any],
) -> NormalizedProject:
    if isinstance(source, dict):
        project = normalize_document(source, source_path=None)
    else:
        project = normalize_file(source)
    if not project.source_schema == version:
        raise NormalizationError(
            f"schema labelled {version} was detected as {project.source_schema}"
        )
    return project


def verify_project_set(
    sources: dict[str, str | Path | dict[str, Any]],
) -> dict[str, Any]:
    """Normalize, route, compare and validate one three-version project set."""

    if set(sources) != {"v1", "v2", "v3"}:
        raise ValueError("sources must contain exactly v1, v2 and v3")
    versions: dict[str, Any] = {}
    normalization: dict[str, Any] = {}
    schema_files: dict[str, str] = {}
    fingerprints: dict[str, str] = {}
    projects: dict[str, NormalizedProject] = {}
    source_metadata: dict[str, dict[str, Any]] = {}

    for version in ("v1", "v2", "v3"):
        source = sources[version]
        if isinstance(source, dict):
            document = source
        else:
            try:
                document = json.loads(
                    Path(source).read_text(encoding="utf-8")
                )
            except (OSError, json.JSONDecodeError) as exc:
                raise NormalizationError(
                    f"cannot read schema {source}: {exc}"
                ) from exc
        source_metadata[version] = dict(document.get("metadata", {}))
        project = _normalize_source(version, source)
        projects[version] = project
        main = run_allocation(project)
        reference = run_reference(project)
        check = compare_with_reference(main, reference)
        aggregation = _ledger_aggregation_checks(project, main)
        fingerprint = canonical_fingerprint(project)
        fingerprints[version] = fingerprint
        normalization[version] = project.to_dict()
        if isinstance(source, (str, Path)):
            schema_files[version] = Path(source).name
        else:
            schema_files[version] = f"project_{version}.json"
        versions[version] = {
            "normalized_fingerprint": fingerprint,
            "reference": reference,
            "reference_check": check,
            "route": main,
            "ledger_aggregation": aggregation,
            "normalized": project.to_dict(),
        }

    first_project = projects["v1"]
    version_signatures = {
        version: versions[version]["route"]["assignment_signature"]
        for version in ("v1", "v2", "v3")
    }
    version_ledgers = {
        version: versions[version]["route"]["after_rebalance"]["ledger"]
        for version in ("v1", "v2", "v3")
    }
    version_metrics = {
        version: versions[version]["route"]["after_rebalance"]["metrics"]
        for version in ("v1", "v2", "v3")
    }
    version_events = {
        version: [
            _event_signature(event_result)
            for event_result in versions[version]["route"]["event_results"]
        ]
        for version in ("v1", "v2", "v3")
    }
    baseline = "v1"
    canonical_equal = len(set(fingerprints.values())) == 1
    routes_equal = all(
        _canonical_compact(version_signatures[version])
        == _canonical_compact(version_signatures[baseline])
        for version in ("v2", "v3")
    )
    ledgers_equal = all(
        _canonical_compact(version_ledgers[version])
        == _canonical_compact(version_ledgers[baseline])
        for version in ("v2", "v3")
    )
    metrics_equal = all(
        _canonical_compact(version_metrics[version])
        == _canonical_compact(version_metrics[baseline])
        for version in ("v2", "v3")
    )
    events_equal = all(
        _canonical_compact(version_events[version])
        == _canonical_compact(version_events[baseline])
        for version in ("v2", "v3")
    )

    observable_differences: list[dict[str, Any]] = []
    if not canonical_equal:
        for version in ("v2", "v3"):
            if fingerprints[version] != fingerprints[baseline]:
                observable_differences.append(
                    {
                        "aspect": "canonical_model_fingerprint",
                        "baseline": fingerprints[baseline],
                        "other": fingerprints[version],
                        "source_schema": version,
                        "stage": "normalization",
                        "evidence": {
                            "canonical_model": projects[version].canonical_dict(),
                            "baseline_canonical_model": projects[
                                baseline
                            ].canonical_dict(),
                        },
                    }
                )
    if canonical_equal and not routes_equal:
        for version in ("v2", "v3"):
            difference = _first_route_difference(
                versions[baseline]["route"], versions[version]["route"]
            )
            if difference:
                observable_differences.append(
                    {
                        "aspect": "per_request_assignment",
                        "baseline": baseline,
                        "other": version,
                        "source_schema": version,
                        "stage": "routing",
                        "evidence": difference,
                    }
                )
    if canonical_equal and not ledgers_equal:
        observable_differences.append(
            {
                "aspect": "replica_ledger",
                "baseline": baseline,
                "other": "v2/v3",
                "stage": "routing",
                "evidence": "canonical models match but aggregate ledgers differ",
            }
        )
    if canonical_equal and not metrics_equal:
        observable_differences.append(
            {
                "aspect": "imbalance_metrics",
                "baseline": baseline,
                "other": "v2/v3",
                "stage": "routing",
                "evidence": "canonical models match but imbalance metrics differ",
            }
        )
    if canonical_equal and not events_equal:
        observable_differences.append(
            {
                "aspect": "event_recomputation",
                "baseline": baseline,
                "other": "v2/v3",
                "stage": "routing",
                "evidence": "canonical models match but event result signatures differ",
            }
        )

    resolved_normalization_differences = []
    for version in ("v1", "v2", "v3"):
        for item in projects[version].normalization_evidence:
            resolved_normalization_differences.append(
                {
                    **item,
                    "observable_after_normalization": False,
                }
            )

    reference_checks = {
        version: {
            "agrees": versions[version]["reference_check"]["agrees"],
            "first_mismatch_request": versions[version]["reference_check"][
                "first_mismatch_request"
            ],
        }
        for version in ("v1", "v2", "v3")
    }
    reference_all = all(
        versions[version]["reference_check"]["agrees"]
        for version in ("v1", "v2", "v3")
    )
    capacity_ok = all(
        _final_capacity_ok(versions[version]["route"])
        for version in ("v1", "v2", "v3")
    )
    traces_complete = all(
        _trace_complete(versions[version]["route"])
        for version in ("v1", "v2", "v3")
    )
    aggregation_ok = all(
        versions[version]["ledger_aggregation"]["ok"]
        for version in ("v1", "v2", "v3")
    )
    events_recomputed = all(
        len(versions[version]["route"]["event_results"])
        == len(projects[version].events)
        for version in ("v1", "v2", "v3")
    )
    event_has_affected = all(
        any(
            event["affected_request_ids"]
            for event in versions[version]["route"]["event_results"]
        )
        for version in ("v1", "v2", "v3")
    )
    event_feasible = all(
        all(
            event["feasible_after_event"]
            for event in versions[version]["route"]["event_results"]
        )
        for version in ("v1", "v2", "v3")
    )
    rebalance_non_worse = all(
        _rebalance_non_worse(versions[version]["route"])
        for version in ("v1", "v2", "v3")
    )
    cross_version_consistent = (
        canonical_equal
        and routes_equal
        and ledgers_equal
        and metrics_equal
        and events_equal
        and not observable_differences
    )
    checks = {
        "all_events_recomputed": events_recomputed,
        "at_least_one_event_has_affected_requests": event_has_affected,
        "capacity_constraints_satisfied": capacity_ok,
        "cross_version_consistent": cross_version_consistent,
        "event_recomputations_capacity_feasible": event_feasible,
        "ledger_aggregation_consistent": aggregation_ok,
        "per_request_trace_complete": traces_complete,
        "rebalance_non_worse": rebalance_non_worse,
        "reference_allocator_agrees_for_all_versions": reference_all,
    }
    acceptance = {
        **checks,
        "passed": all(checks.values()),
        "reference_checks": reference_checks,
        "requests_checked_per_version": {
            version: versions[version]["reference_check"]["requests_checked"]
            for version in ("v1", "v2", "v3")
        },
        "versions_checked": ["v1", "v2", "v3"],
    }
    return {
        "acceptance": acceptance,
        "cross_version": {
            "baseline_version": baseline,
            "canonical_fingerprints": fingerprints,
            "canonical_models_equal": canonical_equal,
            "consistent": cross_version_consistent,
            "event_recomputations_equal": events_equal,
            "imbalance_metrics_equal": metrics_equal,
            "ledgers_equal": ledgers_equal,
            "observable_differences": observable_differences,
            "per_request_routes_equal": routes_equal,
            "resolved_normalization_differences": (
                resolved_normalization_differences
            ),
            "version_route_snapshots": version_signatures,
        },
        "metadata": {
            "generator": source_metadata["v1"].get("generator"),
            "reference_sequence_length": int(
                first_project.normalization["reference_sequence_length"]
            ),
            "request_count": len(first_project.requests),
            "sample_equivalent_bytes": int(
                first_project.normalization["sample_equivalent_bytes"]
            ),
            "seed": source_metadata["v1"].get("seed"),
        },
        "normalization": normalization,
        "schema_files": schema_files,
        "source_metadata": source_metadata,
        "versions": versions,
    }

