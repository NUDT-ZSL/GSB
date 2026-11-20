from __future__ import annotations

import copy
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any

import numpy as np

from .errors import Issue
from .jsonutil import canonical_json, is_number, sha256_hex
from .model import KIND_ORDER, EngineConfig, Resource

SIMPLE_PATH_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


@dataclass(frozen=True)
class CompareDecision:
    mode: str
    namespace_object_counts: dict[str, int]
    max_field_count: int
    max_field_resource: str | None
    triggers: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "exact_object_threshold": None,
            "exact_field_threshold": None,
            "namespace_object_counts": dict(
                sorted(self.namespace_object_counts.items())
            ),
            "max_field_count": self.max_field_count,
            "max_field_resource": self.max_field_resource,
            "triggers": list(self.triggers),
        }


def _field_path(parent: str, key: str | int) -> str:
    if isinstance(key, int):
        return f"{parent}[{key}]"
    if SIMPLE_PATH_KEY.fullmatch(key):
        return f"{parent}.{key}"
    return f"{parent}[{canonical_json(key)}]"


def flatten_value(value: Any, path: str = "$") -> dict[str, Any]:
    fields: dict[str, Any] = {}
    if isinstance(value, dict):
        if not value:
            fields[path] = {}
            return fields
        for key in sorted(value):
            fields.update(flatten_value(value[key], _field_path(path, key)))
        return fields
    if isinstance(value, list):
        if not value:
            fields[path] = []
            return fields
        for index, item in enumerate(value):
            fields.update(flatten_value(item, _field_path(path, index)))
        return fields
    fields[path] = value
    return fields


def resource_fields(resource: Resource) -> dict[str, Any]:
    body = copy.deepcopy(resource.body)
    for key in ("kind", "namespace", "name"):
        body.pop(key, None)
    return flatten_value(body)


def _field_count(resource: Resource) -> int:
    return len(resource_fields(resource))


def choose_algorithm(
    desired: dict[str, Resource],
    actual: dict[str, Resource],
    config: EngineConfig,
) -> CompareDecision:
    all_resources: dict[str, Resource] = {}
    all_resources.update(desired)
    all_resources.update(actual)

    namespace_counts: Counter[str] = Counter()
    for resource in all_resources.values():
        namespace_counts[resource.namespace] += 1

    max_fields = 0
    max_resource: str | None = None
    for resource in sorted(
        all_resources.values(), key=lambda item: item.sort_key
    ):
        count = _field_count(resource)
        if count > max_fields:
            max_fields = count
            max_resource = resource.key

    triggers: list[dict[str, Any]] = []
    for namespace, count in sorted(namespace_counts.items()):
        if count > config.exact_object_threshold:
            triggers.append(
                {
                    "condition": "namespace_object_count",
                    "namespace": namespace,
                    "observed": count,
                    "threshold": config.exact_object_threshold,
                    "expression": (
                        f"{count} > {config.exact_object_threshold}"
                    ),
                }
            )
    if max_fields > config.exact_field_threshold:
        triggers.append(
            {
                "condition": "object_field_count",
                "resource": max_resource,
                "observed": max_fields,
                "threshold": config.exact_field_threshold,
                "expression": f"{max_fields} > {config.exact_field_threshold}",
            }
        )
    return CompareDecision(
        mode="digest_sampled" if triggers else "exact",
        namespace_object_counts=dict(namespace_counts),
        max_field_count=max_fields,
        max_field_resource=max_resource,
        triggers=tuple(triggers),
    )


def _numeric_equal_with_tolerance(
    expected: Any, actual: Any, tolerance: float
) -> bool:
    return (
        is_number(expected)
        and is_number(actual)
        and abs(float(expected) - float(actual)) <= tolerance
    )


def _drift_type(path: str) -> str:
    if ".selector" in path:
        return "selector_mismatch"
    if path == "$.replicas":
        return "replicas_mismatch"
    return "field_mismatch"


def _make_field_drift(
    resource: Resource,
    path: str,
    expected: Any,
    actual: Any,
    *,
    expected_present: bool,
    actual_present: bool,
    basis: dict[str, Any],
) -> dict[str, Any]:
    return {
        "identity": resource.identity,
        "type": _drift_type(path),
        "field_path": path,
        "expected_present": expected_present,
        "actual_present": actual_present,
        "expected": expected,
        "actual": actual,
        "basis": basis,
    }


def _compare_object_fields(
    expected: Resource,
    actual: Resource,
    config: EngineConfig,
    *,
    basis_factory: Any,
) -> list[dict[str, Any]]:
    expected_fields = resource_fields(expected)
    actual_fields = resource_fields(actual)
    drifts: list[dict[str, Any]] = []
    for path in sorted(set(expected_fields) | set(actual_fields)):
        expected_present = path in expected_fields
        actual_present = path in actual_fields
        if expected_present and actual_present:
            expected_value = expected_fields[path]
            actual_value = actual_fields[path]
            if expected_value == actual_value:
                continue
            if (
                config.tolerance > 0
                and path != "$.replicas"
                and _numeric_equal_with_tolerance(
                    expected_value, actual_value, config.tolerance
                )
            ):
                continue
        else:
            expected_value = expected_fields.get(path)
            actual_value = actual_fields.get(path)
        drifts.append(
            _make_field_drift(
                expected,
                path,
                expected_value,
                actual_value,
                expected_present=expected_present,
                actual_present=actual_present,
                basis=basis_factory(path),
            )
        )
    return drifts


def _exact_compare(
    expected: Resource,
    actual: Resource,
    config: EngineConfig,
) -> list[dict[str, Any]]:
    def basis_factory(path: str) -> dict[str, Any]:
        return {
            "kind": "exact_field",
            "path": path,
            "tolerance": config.tolerance,
            "tolerance_applied": config.tolerance > 0,
        }

    return _compare_object_fields(
        expected, actual, config, basis_factory=basis_factory
    )


def _sha(value: Any) -> str:
    return sha256_hex(value)


def _select_strata(
    strata: dict[str, list[str]], ratio: float
) -> tuple[list[str], list[str]]:
    if not strata:
        return [], []
    ordered_strata = sorted(strata)
    target = min(
        len(ordered_strata),
        max(1, math.ceil(len(ordered_strata) * ratio)),
    )
    ranks = {
        stratum: _sha({"sampling": "stratum", "name": stratum})
        for stratum in ordered_strata
    }
    selected: list[str] = []

    size_layers = sorted({stratum.split("|", 1)[0] for stratum in ordered_strata})
    for layer in size_layers:
        candidates = [
            stratum
            for stratum in ordered_strata
            if stratum.split("|", 1)[0] == layer
        ]
        if candidates and len(selected) < target:
            selected.append(min(candidates, key=lambda item: ranks[item]))
    for stratum in sorted(ordered_strata, key=lambda item: (ranks[item], item)):
        if len(selected) >= target:
            break
        if stratum not in selected:
            selected.append(stratum)
    selected_set = set(selected)
    not_selected = [
        stratum for stratum in ordered_strata if stratum not in selected_set
    ]
    return sorted(selected), not_selected


def _digest_compare_object(
    expected: Resource,
    actual: Resource,
    config: EngineConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    expected_fields = resource_fields(expected)
    actual_fields = resource_fields(actual)
    paths = sorted(set(expected_fields) | set(actual_fields))
    lengths = [
        len(canonical_json(expected_fields.get(path)))
        + len(canonical_json(actual_fields.get(path)))
        for path in paths
    ]
    if lengths:
        quantiles = np.quantile(
            np.asarray(lengths, dtype=np.int64), [0.33, 0.66]
        ).tolist()
    else:
        quantiles = [0.0, 0.0]
    low = float(quantiles[0])
    high = float(quantiles[1])

    entries: list[dict[str, Any]] = []
    strata: dict[str, list[str]] = {}
    stratum_for_path: dict[str, str] = {}
    for path, length in zip(paths, lengths):
        if length <= low:
            size_layer = "small"
        elif length <= high:
            size_layer = "medium"
        else:
            size_layer = "large"
        bucket_count = max(
            1, min(config.strata_count, max(1, len(paths)))
        )
        bucket = int(_sha({"path": path})[:8], 16) % bucket_count
        depth = path.count(".") + path.count("[")
        stratum = f"{size_layer}|depth{depth}|bucket{bucket}"
        stratum_for_path[path] = stratum
        strata.setdefault(stratum, []).append(path)
        entries.append(
            {
                "path": path,
                "expected_present": path in expected_fields,
                "actual_present": path in actual_fields,
                "expected_sha256": _sha(expected_fields.get(path)),
                "actual_sha256": _sha(actual_fields.get(path)),
            }
        )
    root_digest = _sha(
        {
            "identity": expected.identity,
            "fields": entries,
            "quantiles": quantiles,
        }
    )
    if not any(
        item["expected_sha256"] != item["actual_sha256"]
        or item["expected_present"] != item["actual_present"]
        for item in entries
    ):
        return [], {
            "kind": "hierarchical_digest",
            "identity": expected.identity,
            "root_digest": root_digest,
            "field_count": len(paths),
            "stratum_count": len(strata),
            "length_quantiles": quantiles,
            "sampling_ratio": config.sampling_ratio,
            "configured_strata_count": config.strata_count,
            "sampled_strata": [],
            "completion_strata": [],
            "sample_only_diff_count": 0,
            "completion_diff_count": 0,
            "sampling_coverage": 1.0,
            "expected_single_stratum_detection_probability": 1.0,
            "final_recall_estimate": 1.0,
            "false_negative_upper_bound": 0.0,
            "root_equal": True,
        }

    sampled_strata, completion_strata = _select_strata(
        strata, config.sampling_ratio
    )
    drifts: list[dict[str, Any]] = []
    sample_diff_paths: set[str] = set()

    def process_paths(
        paths_to_process: list[str], *, in_sample: bool
    ) -> None:
        for path in paths_to_process:
            expected_present = path in expected_fields
            actual_present = path in actual_fields
            if expected_present and actual_present:
                expected_value = expected_fields[path]
                actual_value = actual_fields[path]
                if expected_value == actual_value:
                    continue
                if (
                    config.tolerance > 0
                    and path != "$.replicas"
                    and _numeric_equal_with_tolerance(
                        expected_value,
                        actual_value,
                        config.tolerance,
                    )
                ):
                    continue
            else:
                expected_value = expected_fields.get(path)
                actual_value = actual_fields.get(path)
            if in_sample:
                sample_diff_paths.add(path)
            drifts.append(
                _make_field_drift(
                    expected,
                    path,
                    expected_value,
                    actual_value,
                    expected_present=expected_present,
                    actual_present=actual_present,
                    basis={
                        "kind": "hierarchical_digest",
                        "stratum": stratum_for_path[path],
                        "found_in_sample": in_sample,
                        "completion_pass": not in_sample,
                        "expected_sha256": _sha(expected_value),
                        "actual_sha256": _sha(actual_value),
                        "tolerance": config.tolerance,
                    },
                )
            )

    for stratum in sampled_strata:
        process_paths(strata[stratum], in_sample=True)
    for stratum in completion_strata:
        process_paths(strata[stratum], in_sample=False)
    drifts.sort(key=lambda item: (item["field_path"], item["type"]))
    completion_diff_count = len(drifts) - len(sample_diff_paths)
    evidence = {
        "kind": "hierarchical_digest",
        "identity": expected.identity,
        "root_digest": root_digest,
        "field_count": len(paths),
        "stratum_count": len(strata),
        "length_quantiles": quantiles,
        "sampling_ratio": config.sampling_ratio,
        "configured_strata_count": config.strata_count,
        "sampled_strata": sampled_strata,
        "completion_strata": completion_strata,
        "sample_only_diff_count": len(sample_diff_paths),
        "completion_diff_count": completion_diff_count,
            "sampling_coverage": (
                len(sampled_strata) / len(strata) if strata else 1.0
            ),
            "expected_single_stratum_detection_probability": (
                len(sampled_strata) / len(strata) if strata else 1.0
            ),
        "sample_only_recall_estimate": (
            len(sample_diff_paths) / len(drifts) if drifts else 1.0
        ),
        "final_recall_estimate": 1.0,
        "false_negative_upper_bound": 0.0,
        "root_equal": False,
        "fallback_reason": "root_digest_mismatch_requires_complete_traversal",
    }
    return drifts, evidence


def _drift_signature(drift: dict[str, Any]) -> tuple[str, ...]:
    return (
        drift["identity"]["kind"],
        drift["identity"]["namespace"],
        drift["identity"]["name"],
        drift["type"],
        drift["field_path"],
        canonical_json(drift["expected"]),
        canonical_json(drift["actual"]),
        str(drift["expected_present"]),
        str(drift["actual_present"]),
    )


def _compare_identity_set(
    desired: dict[str, Resource],
    actual: dict[str, Resource],
    config: EngineConfig,
    *,
    mode: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    drifts: list[dict[str, Any]] = []
    digest_evidence: list[dict[str, Any]] = []
    all_keys = sorted(
        set(desired) | set(actual),
        key=lambda key: (
            key.split("/", 2)[1],
            KIND_ORDER.get(key.split("/", 2)[0], 99),
            key.split("/", 2)[2],
            key,
        ),
    )
    for key in all_keys:
        expected = desired.get(key)
        actual_value = actual.get(key)
        if expected is None:
            assert actual_value is not None
            drifts.append(
                {
                    "identity": actual_value.identity,
                    "type": "extra",
                    "field_path": "$",
                    "expected_present": False,
                    "actual_present": True,
                    "expected": None,
                    "actual": actual_value.body,
                    "basis": {
                        "kind": "identity_match",
                        "matched": False,
                        "reason": "actual object has no desired counterpart",
                    },
                }
            )
            continue
        if actual_value is None:
            drifts.append(
                {
                    "identity": expected.identity,
                    "type": "missing",
                    "field_path": "$",
                    "expected_present": True,
                    "actual_present": False,
                    "expected": expected.body,
                    "actual": None,
                    "basis": {
                        "kind": "identity_match",
                        "matched": False,
                        "reason": "desired object has no actual counterpart",
                    },
                }
            )
            continue
        if mode == "exact":
            identity_drifts = _exact_compare(expected, actual_value, config)
        else:
            identity_drifts, evidence = _digest_compare_object(
                expected, actual_value, config
            )
            digest_evidence.append(evidence)
        drifts.extend(identity_drifts)
    drifts.sort(
        key=lambda item: (
            item["identity"]["namespace"],
            KIND_ORDER.get(item["identity"]["kind"], 99),
            item["identity"]["name"],
            item["field_path"],
            item["type"],
        )
    )
    digest_evidence.sort(
        key=lambda item: (
            item["identity"]["namespace"],
            KIND_ORDER.get(item["identity"]["kind"], 99),
            item["identity"]["name"],
        )
    )
    return drifts, digest_evidence


def compute_diff(
    desired: dict[str, Resource],
    actual: dict[str, Resource],
    issues: list[Issue],
    config: EngineConfig,
) -> dict[str, Any]:
    decision = choose_algorithm(desired, actual, config)
    drifts, evidence = _compare_identity_set(
        desired, actual, config, mode=decision.mode
    )

    equivalence: dict[str, Any]
    if decision.mode == "digest_sampled" and config.equivalence_audit:
        exact_drifts, _ = _compare_identity_set(
            desired, actual, config, mode="exact"
        )
        digest_signatures = [_drift_signature(item) for item in drifts]
        exact_signatures = [_drift_signature(item) for item in exact_drifts]
        equivalent = digest_signatures == exact_signatures
        equivalence = {
            "required": True,
            "equivalent": equivalent,
            "digest_mode_drift_count": len(drifts),
            "exact_mode_drift_count": len(exact_drifts),
            "mismatches": (
                []
                if equivalent
                else [
                    {
                        "digest_only": [
                            list(item)
                            for item in digest_signatures
                            if item not in set(exact_signatures)
                        ],
                        "exact_only": [
                            list(item)
                            for item in exact_signatures
                            if item not in set(digest_signatures)
                        ],
                    }
                ]
            ),
            "conclusion": (
                "digest_sampled and exact modes produced identical "
                "drift classifications"
                if equivalent
                else "digest_sampled and exact modes disagree"
            ),
        }
    elif decision.mode == "digest_sampled":
        equivalence = {
            "required": True,
            "equivalent": None,
            "conclusion": "equivalence audit disabled by configuration",
        }
    else:
        equivalence = {
            "required": False,
            "equivalent": True,
            "conclusion": "exact mode is the reference implementation",
        }

    decision_dict = decision.to_dict()
    decision_dict["exact_object_threshold"] = config.exact_object_threshold
    decision_dict["exact_field_threshold"] = config.exact_field_threshold
    decision_dict["digest_evidence"] = evidence

    type_counts = Counter(drift["type"] for drift in drifts)
    return {
        "algorithm": decision_dict,
        "drifts": drifts,
        "equivalence": equivalence,
        "validation_issues": [issue.to_dict() for issue in issues],
        "summary": {
            "drift_count": len(drifts),
            "by_type": dict(sorted(type_counts.items())),
            "validation_issue_count": len(issues),
            "affected_resources": len(
                {
                    (
                        drift["identity"]["kind"],
                        drift["identity"]["namespace"],
                        drift["identity"]["name"],
                    )
                    for drift in drifts
                }
            ),
        },
    }

