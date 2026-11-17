"""Canonical report construction and atomic file replacement."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .manifest import (
    HASH_ALGORITHMS,
    HASH_ALGORITHM_WHITELIST_VERSION,
)
REPORT_SCHEMA_VERSION = "1.0"
GRAPH_SCHEMA_VERSION = "1.0"


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def atomic_write_json(path: str | Path, value: Any, *, pretty: bool = True) -> None:
    """Write JSON beside the destination, fsync, then replace atomically."""

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        indent=2 if pretty else None,
        separators=None if pretty else (",", ":"),
        allow_nan=False,
    )
    if pretty:
        payload += "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=str(destination.parent),
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, destination)
    except BaseException:
        try:
            temporary_path.unlink(missing_ok=True)
        finally:
            raise


def _as_result(item: Any) -> dict:
    if isinstance(item, dict):
        return item
    return item.to_dict()


def _manifest_sort_key(item: Any) -> tuple:
    result = _as_result(item)
    return (
        result.get("project_id") or "",
        result.get("manifest_version") or "",
        result.get("content_digest") or "",
        result.get("source") or "",
    )


def _summary(results: Iterable[dict], reused: int) -> dict:
    items = list(results)
    component_summaries = [item.get("counts", {}) for item in items]

    def total(field: str) -> int:
        return sum(int(summary.get(field, 0)) for summary in component_summaries)

    return {
        "manifest_count": len(items),
        "successful_manifest_count": sum(item.get("status") == "success" for item in items),
        "partial_manifest_count": sum(item.get("status") == "partial" for item in items),
        "failed_manifest_count": sum(item.get("status") == "failed" for item in items),
        "checkpoint_reused_manifest_count": reused,
        "component_count": total("component_count"),
        "complete_component_count": total("complete_count"),
        "incomplete_component_count": total("incomplete_count"),
        "indeterminate_component_count": total("indeterminate_count"),
        "satisfiable_count": total("satisfiable_count"),
        "unsatisfiable_count": total("unsatisfiable_count"),
        "uncertain_count": total("uncertain_count"),
        "signature_missing_count": total("signature_missing_count"),
        "rejected_entry_count": total("rejected_entry_count"),
        "cycle_count": total("cycle_count"),
        "truth_checked_component_count": sum(
            item.get("truth_check", {}).get("checked_components", 0) for item in items
        ),
        "truth_consistent_count": sum(
            item.get("truth_check", {}).get("consistent_count", 0) for item in items
        ),
        "truth_inconsistent_count": sum(
            item.get("truth_check", {}).get("inconsistent_count", 0) for item in items
        ),
    }


def build_run_id(results: Iterable[Any]) -> str:
    identities = sorted(
        (
            result.get("project_id") or "",
            result.get("manifest_version") or "",
            result.get("content_digest") or "",
        )
        for result in (_as_result(item) for item in results)
    )
    return digest_json(
        {
            "schema": REPORT_SCHEMA_VERSION,
            "whitelist_version": HASH_ALGORITHM_WHITELIST_VERSION,
            "manifests": identities,
        }
    )


def build_report(
    diagnostics: Iterable[Any],
    *,
    checkpoint: dict | None = None,
    reused_manifest_count: int = 0,
) -> dict:
    ordered = [_as_result(item) for item in diagnostics]
    ordered.sort(key=_manifest_sort_key)
    run_id = build_run_id(ordered)
    manifest_results = list(ordered)
    uncertain_items = []
    for item in ordered:
        for uncertain in item.get("uncertain_items", []):
            uncertain_items.append(
                {
                    "source": item.get("source"),
                    "project_id": item.get("project_id"),
                    "manifest_version": item.get("manifest_version"),
                    **uncertain,
                }
            )
    uncertain_items.sort(
        key=lambda item: (
            item.get("project_id") or "",
            item.get("component_id") or "",
            item.get("reason") or "",
        )
    )
    summary = _summary(ordered, reused_manifest_count)
    exit_relevant_failure = (
        summary["partial_manifest_count"] + summary["failed_manifest_count"] > 0
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool_version": "1.0.0",
        "run_id": run_id,
        "hash_algorithm_whitelist": {
            "version": HASH_ALGORITHM_WHITELIST_VERSION,
            "algorithms": {
                name: {"digest_bits": bits}
                for name, bits in sorted(HASH_ALGORITHMS.items())
            },
        },
        "checkpoint": checkpoint
        or {
            "mode": "disabled",
            "reused_manifest_count": reused_manifest_count,
        },
        "conclusion": "partial_failure" if exit_relevant_failure else "all_success",
        "exit_code": 1 if exit_relevant_failure else 0,
        "summary": summary,
        "uncertain_items": uncertain_items,
        "manifests": manifest_results,
    }


def build_graph_data(
    diagnostics: Iterable[Any],
    *,
    run_id: str | None = None,
) -> dict:
    ordered = [_as_result(item) for item in diagnostics]
    ordered.sort(key=_manifest_sort_key)
    graphs = []
    for result in ordered:
        nodes = []
        components_by_id = {
            component["component_id"]: component
            for component in result.get("components", [])
        }
        for component_id in sorted(components_by_id):
            component = components_by_id[component_id]
            nodes.append(
                {
                    "id": component_id,
                    "entry_index": component.get("entry_index"),
                    "version": component.get("normalized_version"),
                    "constraint_status": component.get("version_status"),
                    "integrity_status": component.get("integrity_status"),
                    "complete": component.get("complete"),
                    "cycle_member": component.get("cycle_member"),
                    "hash": {
                        "algorithm": component.get("hash", {}).get("algorithm"),
                        "status": component.get("hash", {}).get("status"),
                        "rounding_error": component.get("error_bounds", {})
                        .get("hash_rounding", {})
                        .get("per_link"),
                    },
                    "signature": {
                        "status": component.get("signature", {}).get("status"),
                        "missing": any(
                            component.get("signature", {}).get("missing_by", [])
                        ),
                        "missing_by": component.get("signature", {}).get("missing_by", []),
                    },
                    "error_bounds": {
                        "version_local": component.get("error_bounds", {})
                        .get("version", {})
                        .get("local_width"),
                        "version_cumulative": component.get("error_bounds", {})
                        .get("version", {})
                        .get("cumulative_width"),
                        "hash_rounding_cumulative": component.get("error_bounds", {})
                        .get("hash_rounding", {})
                        .get("cumulative"),
                        "path": component.get("error_bounds", {})
                        .get("version", {})
                        .get("path", []),
                    },
                    "errors": [
                        error.get("code") for error in component.get("errors", [])
                    ],
                    "warnings": [
                        warning.get("code") for warning in component.get("warnings", [])
                    ],
                }
            )
        target_status = {
            component_id: component.get("version_status")
            for component_id, component in components_by_id.items()
        }
        edge_map: dict[tuple, dict] = {}
        for component_id, component in components_by_id.items():
            for incoming in component.get("incoming_constraints", []):
                edge = {
                    "source": incoming["source"],
                    "target": component_id,
                    "edge_index": incoming["edge_index"],
                    "constraint": incoming["canonical"],
                    "declared_constraint": incoming.get("declared"),
                }
                key = (
                    edge["source"],
                    edge["target"],
                    edge["constraint"],
                    json.dumps(
                        edge["declared_constraint"],
                        sort_keys=True,
                        ensure_ascii=False,
                        default=str,
                    ),
                    edge["edge_index"],
                )
                edge_map[key] = edge
        edges = []
        for key in sorted(edge_map, key=lambda item: tuple(str(part) for part in item)):
            edge = dict(edge_map[key])
            edge["target_constraint_status"] = target_status.get(edge["target"])
            edge["satisfiable"] = target_status.get(edge["target"]) == "satisfiable"
            edges.append(edge)
        graphs.append(
            {
                "source": result.get("source"),
                "project_id": result.get("project_id"),
                "manifest_version": result.get("manifest_version"),
                "content_digest": result.get("content_digest"),
                "status": result.get("status"),
                "conclusion": result.get("conclusion"),
                "version_resolution": result.get("version_resolution"),
                "errors": result.get("errors", []),
                "roots": result.get("roots", []),
                "cycles": result.get("cycles", []),
                "nodes": nodes,
                "edges": edges,
                "traversal": {
                    "dependency_edge_direction": "source -> target",
                    "signature_propagation_direction": "dependency -> downstream consumer",
                    "cycle_enumeration_truncated": result.get(
                        "cycle_enumeration_truncated", False
                    ),
                    "path_enumeration_truncated": result.get(
                        "path_enumeration_truncated", False
                    ),
                },
            }
        )
    return {
        "schema_version": GRAPH_SCHEMA_VERSION,
        "tool_version": "1.0.0",
        "run_id": run_id or build_run_id(ordered),
        "graphs": graphs,
    }

