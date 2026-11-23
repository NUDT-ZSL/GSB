from __future__ import annotations

from pathlib import Path

from lbworkbench.merge import merge_snapshot
from lbworkbench.snapshots import parse_package


def test_cross_version_merge_and_manual_adjudication(samples: Path) -> None:
    merged = merge_snapshot(parse_package(samples / "package-2026.11.1.json"))
    by_id = {item["instance_id"]: item for item in merged["instances"]}

    assert by_id["api-01"]["selected_source"] == "v2"
    assert by_id["api-01"]["health_status"] == "healthy"
    assert by_id["api-01"]["decision"]["selected_source"] == "v2"
    assert by_id["api-01"]["sources"]["v1"]["raw"]["instance_id"] == "API-01"
    assert by_id["api-01"]["sources"]["v2"]["raw"]["canonical_instance_id"] == "api-01"

    cache = by_id["cache-01"]
    assert cache["selected_source"] == "v1"
    conversion = cache["sources"]["v1"]["time_conversion"]
    assert conversion["logical_clock_ms"] == 1_764_000_000_000
    assert conversion["scale_ms_per_second"] == 1000
    assert conversion["rounding"] == "exact_integer"
    assert conversion["rounding_direction"] == "none"
    assert conversion["output_value"] == 1_763_999_970_000
    assert cache["sources"]["v1"]["time_representation"] == "relative_seconds"

    manual = by_id["manual-01"]
    assert manual["manual"] is True
    assert manual["health_status"] == "pending_manual"
    assert manual["selected_source"] is None
    assert manual["sources"]["v1"]["raw"]["status"] == "healthy"
    assert manual["sources"]["v2"]["raw"]["status"] == "unhealthy"

    assert by_id["edge-01"]["sources"].keys() == {"v2"}
    assert by_id["tie-01"]["selected_source"] == "v2"
    assert "equivalent source semantics" in by_id["tie-01"]["decision"]["rule"]


def test_merge_is_deterministic(samples: Path) -> None:
    first = merge_snapshot(parse_package(samples / "package-2026.11.1.json"))
    second = merge_snapshot(parse_package(samples / "package-2026.11.1.json"))
    assert first["merged_digest"] == second["merged_digest"]
    assert first["instances"] == second["instances"]
