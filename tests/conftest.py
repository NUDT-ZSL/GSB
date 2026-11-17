from __future__ import annotations

from typing import Any

from sensitive_guard.model import compute_sync_marker


def make_record(
    path: str,
    sources: list[str] | None = None,
    status: str = "masked",
    *,
    version: str = "v2",
    transitive: bool = True,
    policy_id: str = "policy.test",
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "path": path,
        "sources": sources or [],
        "masking": status,
        "policy_id": policy_id,
        "version": version,
    }
    if version == "v2":
        record["transitive"] = transitive
    return record


def make_dataset(
    records: list[dict[str, Any]],
    *,
    dataset_id: str = "test",
    injection: dict[str, Any] | None = None,
) -> dict[str, Any]:
    injection = dict(injection or {})
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "sync_marker": compute_sync_marker(records, injection),
        "injection": injection,
        "records": records,
    }


def semantic_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        key: result[key]
        for key in (
            "status",
            "sync_marker_ok",
            "field_count",
            "processed_field_count",
            "codes",
            "rejected_records",
            "duplicate_edges",
            "cycles",
            "violations",
            "fields",
            "observations",
        )
    }
