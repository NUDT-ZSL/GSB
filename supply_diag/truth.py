"""Synchronization-marker based truth comparison.

The checker intentionally ignores any generator-produced diagnostic decision.
It only consumes the manifest's raw truth markers and the normalized values
produced by this workbench.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from typing import Any

from .manifest import ParsedManifest, normalize_hash_algorithm
from .versioning import fraction_to_dict


TRUTH_MARKER_PREFIX = "supply-diag-truth-v1\n"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )


def truth_marker_payload(
    *,
    project_id: str | None,
    component_id: str,
    raw_version_components: Any,
    declared_algorithm: Any,
    expected_error_bound: Any,
) -> dict:
    return {
        "marker_version": "1",
        "project_id": project_id,
        "component_id": component_id,
        "raw_version_components": raw_version_components,
        "declared_algorithm": declared_algorithm,
        "expected_error_bound": expected_error_bound,
    }


def compute_truth_marker(payload: dict) -> str:
    return hashlib.sha256(
        (TRUTH_MARKER_PREFIX + _canonical_json(payload)).encode("utf-8")
    ).hexdigest()


def _fraction_from_any(value: Any) -> Fraction | None:
    if value is None:
        return None
    if isinstance(value, Fraction):
        return value
    if isinstance(value, dict) and "numerator" in value and "denominator" in value:
        return Fraction(int(value["numerator"]), int(value["denominator"]))
    try:
        return Fraction(Decimal(str(value)))
    except (InvalidOperation, ValueError, ZeroDivisionError) as exc:
        raise ValueError(f"invalid bound value: {value!r}") from exc


def _observed_bound(observation: dict, scope: str) -> Fraction | None:
    if scope == "local_version_width":
        return observation.get("local_version_width")
    return observation.get("cumulative_version_width")


def _normalized_components(value: Any) -> list[int] | None:
    if not isinstance(value, (list, tuple)):
        return None
    try:
        return [int(component) for component in value]
    except (TypeError, ValueError):
        return None


def _components_equal(left: Any, right: Any) -> bool:
    left_components = _normalized_components(left)
    right_components = _normalized_components(right)
    if left_components is None or right_components is None:
        return left_components == right_components
    depth = max(len(left_components), len(right_components))
    return (
        left_components + [0] * (depth - len(left_components))
        == right_components + [0] * (depth - len(right_components))
    )


def _normalized_algorithm(value: Any) -> Any:
    try:
        return normalize_hash_algorithm(value)
    except ValueError:
        return value


def verify_manifest_truth(
    manifest: ParsedManifest,
    observations: dict[str, dict],
) -> dict:
    """Compare markers and normalized intervals; never consume inferred truth."""

    mismatches: list[dict] = []
    checked = 0
    unchecked = 0

    for component_id in sorted(manifest.components):
        component = manifest.components[component_id]
        truth = component.truth
        if not truth:
            unchecked += 1
            continue
        checked += 1
        observation = observations.get(component_id, {})

        raw_components = truth.get(
            "raw_version_components",
            truth.get("version_components", truth.get("raw_version")),
        )
        declared_algorithm = truth.get(
            "declared_algorithm", truth.get("algorithm")
        )
        expected_bound = truth.get(
            "expected_error_bound", truth.get("expected_bound")
        )
        marker = truth.get("sync_marker", truth.get("marker"))

        payload = truth_marker_payload(
            project_id=manifest.project_id,
            component_id=component_id,
            raw_version_components=raw_components,
            declared_algorithm=declared_algorithm,
            expected_error_bound=expected_bound,
        )
        expected_marker = compute_truth_marker(payload)
        if not isinstance(marker, str) or marker.lower() != expected_marker:
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": "sync_marker",
                    "expected": expected_marker,
                    "observed": marker,
                }
            )

        observed_components = observation.get("version_components")
        expected_components = (
            list(raw_components) if isinstance(raw_components, (list, tuple)) else None
        )
        if not _components_equal(observed_components, expected_components):
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": "raw_version_components",
                    "expected": expected_components,
                    "observed": observed_components,
                }
            )

        if _normalized_algorithm(observation.get("hash_algorithm")) != _normalized_algorithm(
            declared_algorithm
        ):
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": "declared_algorithm",
                    "expected": declared_algorithm,
                    "observed": observation.get("hash_algorithm"),
                }
            )

        if not isinstance(expected_bound, dict):
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": "expected_error_bound",
                    "expected": "object with lower/upper",
                    "observed": expected_bound,
                }
            )
            continue

        scope = str(expected_bound.get("scope", "cumulative_version_width"))
        observed = _observed_bound(observation, scope)
        try:
            lower = _fraction_from_any(expected_bound.get("lower"))
            upper = _fraction_from_any(expected_bound.get("upper"))
        except ValueError as exc:
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": f"expected_error_bound.{scope}",
                    "expected": expected_bound,
                    "observed": str(exc),
                }
            )
            continue
        if lower is None or upper is None or lower > upper:
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": f"expected_error_bound.{scope}",
                    "expected": {"lower": lower, "upper": upper},
                    "observed": "invalid expected interval",
                }
            )
            continue
        in_interval = observed is not None and lower <= observed <= upper
        if not in_interval:
            mismatches.append(
                {
                    "source": manifest.source_path,
                    "project_id": manifest.project_id,
                    "component_id": component_id,
                    "field": f"normalized_error_bound.{scope}",
                    "expected": {
                        "lower": fraction_to_dict(lower),
                        "upper": fraction_to_dict(upper),
                        "unit": expected_bound.get("unit", "quantum"),
                    },
                    "observed": fraction_to_dict(observed),
                }
            )

    mismatches.sort(
        key=lambda item: (
            str(item.get("source", "")),
            str(item.get("component_id", "")),
            str(item.get("field", "")),
            _canonical_json(item),
        )
    )
    consistent_count = checked - len(
        {
            (item.get("component_id"), item.get("source"))
            for item in mismatches
        }
    )
    inconsistent_count = len(
        {
            (item.get("component_id"), item.get("source"))
            for item in mismatches
        }
    )
    return {
        "checked_components": checked,
        "unchecked_components": unchecked,
        "consistent_count": consistent_count,
        "inconsistent_count": inconsistent_count,
        "observed_consistent_count": consistent_count,
        "observed_inconsistent_count": inconsistent_count,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
    }

