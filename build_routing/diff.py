from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Sequence

from .model import NormalizedProject, Prefix, SelectionResult
from .normalize import normalize_project
from .selector import select_prefix

COMMUNITY = "community_representation"
LOCAL_PREF = "local_pref_scale"

REASON_LABELS = {
    COMMUNITY: "社区标记表达差异",
    LOCAL_PREF: "本地优先级刻度表达差异",
}

PATH_FIELDS = (
    "candidate_id",
    "next_hop_id",
    "as_path",
    "as_path_length",
    "local_pref",
    "communities",
    "community_weight",
    "rank",
)


def _coerce_project(value: NormalizedProject | Mapping[str, Any]) -> NormalizedProject:
    if isinstance(value, NormalizedProject):
        return value
    return normalize_project(value)


def _select(project: NormalizedProject) -> dict[str, SelectionResult]:
    return {
        prefix.id: select_prefix(prefix, project) for prefix in project.prefixes
    }


def _path_value(path: Any, field: str) -> Any:
    if path is None:
        return None
    value = getattr(path, field)
    if isinstance(value, tuple):
        return list(value)
    return value


def _field_diffs(
    prefix_id: str,
    left: SelectionResult,
    right: SelectionResult,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for role in ("best", "backup"):
        left_path = getattr(left, role)
        right_path = getattr(right, role)
        for field in PATH_FIELDS:
            left_value = _path_value(left_path, field)
            right_value = _path_value(right_path, field)
            if left_value != right_value:
                result.append(
                    {
                        "prefix_id": prefix_id,
                        "role": role,
                        "field": f"{role}.{field}",
                        "left": left_value,
                        "right": right_value,
                    }
                )
    if left.status != right.status:
        result.append(
            {
                "prefix_id": prefix_id,
                "role": "prefix",
                "field": "status",
                "left": left.status,
                "right": right.status,
            }
        )
    return result


def _candidate_community_diffs(
    left: Prefix, right: Prefix
) -> list[dict[str, Any]]:
    left_map = {item.id: set(item.communities) for item in left.candidates}
    right_map = {item.id: set(item.communities) for item in right.candidates}
    result: list[dict[str, Any]] = []
    for candidate_id in sorted(set(left_map) | set(right_map)):
        left_set = left_map.get(candidate_id, set())
        right_set = right_map.get(candidate_id, set())
        if left_set != right_set:
            result.append(
                {
                    "candidate_id": candidate_id,
                    "left_communities": sorted(left_set),
                    "right_communities": sorted(right_set),
                    "only_left": sorted(left_set - right_set),
                    "only_right": sorted(right_set - left_set),
                }
            )
    return result


def _candidate_local_pref_diffs(
    left: Prefix, right: Prefix
) -> list[dict[str, Any]]:
    left_map = {item.id: item.local_pref for item in left.candidates}
    right_map = {item.id: item.local_pref for item in right.candidates}
    result: list[dict[str, Any]] = []
    for candidate_id in sorted(set(left_map) | set(right_map)):
        left_value = left_map.get(candidate_id)
        right_value = right_map.get(candidate_id)
        if left_value != right_value:
            result.append(
                {
                    "candidate_id": candidate_id,
                    "left_local_pref": left_value,
                    "right_local_pref": right_value,
                }
            )
    return result


def _normalize_known(
    known: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None,
) -> list[dict[str, Any]]:
    if known is None:
        return []
    if isinstance(known, Mapping):
        items = known.get("items")
        if items is None:
            items = known.get("known_differences")
        if items is None:
            if "prefix_id" in known:
                items = [known]
            else:
                items = []
    else:
        items = list(known)
    return [dict(item) for item in items]


def _expected_match(
    item: Mapping[str, Any],
    left_selection: SelectionResult,
    right_selection: SelectionResult,
) -> bool:
    expected = item.get("expected") or {}
    for side, selection in (("v1", left_selection), ("v2", right_selection)):
        side_expected = expected.get(side)
        if not side_expected:
            continue
        best = selection.best
        actual = {
            "best_next_hop_id": best.next_hop_id if best else None,
            "best_candidate_id": best.candidate_id if best else None,
            "backup_next_hop_id": (
                selection.backup.next_hop_id if selection.backup else None
            ),
            "backup_candidate_id": (
                selection.backup.candidate_id if selection.backup else None
            ),
            "status": selection.status,
        }
        for key, expected_value in side_expected.items():
            if key in actual and actual[key] != expected_value:
                return False
    return True


def compare_projects(
    left: NormalizedProject | Mapping[str, Any],
    right: NormalizedProject | Mapping[str, Any],
    known: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    left_project = _coerce_project(left)
    right_project = _coerce_project(right)
    left_prefixes = left_project.prefix_by_id()
    right_prefixes = right_project.prefix_by_id()
    left_selections = _select(left_project)
    right_selections = _select(right_project)
    known_items = _normalize_known(known)
    known_by_prefix = {str(item["prefix_id"]): item for item in known_items}

    differences: list[dict[str, Any]] = []
    for prefix_id in sorted(set(left_prefixes) | set(right_prefixes)):
        left_prefix = left_prefixes.get(prefix_id)
        right_prefix = right_prefixes.get(prefix_id)
        if left_prefix is None or right_prefix is None:
            differences.append(
                {
                    "prefix_id": prefix_id,
                    "node_id": (
                        left_prefix.build_node_id
                        if left_prefix
                        else right_prefix.build_node_id
                    ),
                    "reasons": ["reference_mismatch"],
                    "reason_labels": ["前缀引用差异"],
                    "field_diffs": [
                        {
                            "field": "prefix.presence",
                            "left": left_prefix is not None,
                            "right": right_prefix is not None,
                        }
                    ],
                    "evidence": {},
                }
            )
            continue

        left_selection = left_selections[prefix_id]
        right_selection = right_selections[prefix_id]
        field_diffs = _field_diffs(prefix_id, left_selection, right_selection)
        if not field_diffs:
            continue

        community_diffs = _candidate_community_diffs(left_prefix, right_prefix)
        local_pref_diffs = _candidate_local_pref_diffs(left_prefix, right_prefix)
        reasons: list[str] = []
        if community_diffs:
            reasons.append(COMMUNITY)
        if local_pref_diffs:
            reasons.append(LOCAL_PREF)
        if not reasons:
            reasons.append("unexplained")

        difference = {
            "prefix_id": prefix_id,
            "node_id": left_prefix.build_node_id,
            "reasons": reasons,
            "reason_labels": [
                REASON_LABELS.get(reason, "未归因的选路差异") for reason in reasons
            ],
            "field_diffs": field_diffs,
            "evidence": {
                "community_candidates": community_diffs,
                "local_pref_candidates": local_pref_diffs,
            },
        }
        known_item = known_by_prefix.get(prefix_id)
        difference["known_difference_match"] = (
            None
            if known_item is None
            else (
                [known_item.get("reason")] == reasons
                and _expected_match(known_item, left_selection, right_selection)
            )
        )
        differences.append(difference)

    actual_by_prefix = {
        item["prefix_id"]: sorted(item["reasons"]) for item in differences
    }
    expected_by_prefix = {
        prefix_id: sorted(
            [
                str(known_by_prefix[prefix_id].get("reason"))
            ]
        )
        for prefix_id in known_by_prefix
    }
    missing_known = sorted(set(expected_by_prefix) - set(actual_by_prefix))
    unexpected = sorted(set(actual_by_prefix) - set(expected_by_prefix))
    reason_mismatch = {
        prefix_id: {
            "expected": expected_by_prefix[prefix_id],
            "actual": actual_by_prefix[prefix_id],
        }
        for prefix_id in sorted(set(expected_by_prefix) & set(actual_by_prefix))
        if expected_by_prefix[prefix_id] != actual_by_prefix[prefix_id]
    }
    expected_value_mismatch = [
        item["prefix_id"]
        for item in differences
        if item.get("known_difference_match") is False
    ]
    known_match = not (
        missing_known
        or unexpected
        or reason_mismatch
        or expected_value_mismatch
    )

    return {
        "left": {
            "project_id": left_project.project_id,
            "schema_version": left_project.schema_version,
            "source_schema": left_project.source_schema,
        },
        "right": {
            "project_id": right_project.project_id,
            "schema_version": right_project.schema_version,
            "source_schema": right_project.source_schema,
        },
        "compared_prefix_count": len(
            set(left_prefixes) & set(right_prefixes)
        ),
        "differences": differences,
        "diff_prefixes": [item["prefix_id"] for item in differences],
        "known_validation": {
            "expected_prefixes": sorted(expected_by_prefix),
            "actual_prefixes": sorted(actual_by_prefix),
            "expected_reasons_by_prefix": expected_by_prefix,
            "actual_reasons_by_prefix": actual_by_prefix,
            "missing_known_prefixes": missing_known,
            "unexpected_prefixes": unexpected,
            "reason_mismatch": reason_mismatch,
            "expected_value_mismatch": expected_value_mismatch,
            "match": known_match,
        },
        "summary": {
            "difference_count": len(differences),
            "known_match": known_match,
            "reasons": {
                reason: sum(1 for item in differences if reason in item["reasons"])
                for reason in (COMMUNITY, LOCAL_PREF, "unexplained")
            },
        },
    }


def load_known(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)
