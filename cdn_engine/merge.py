from __future__ import annotations

from typing import Any, Dict, List, Optional

from .clocking import LogicalClock
from .errors import EngineError, ErrorCode
from .hashing import canonical_text, sha256_object


SOURCE_PRIORITY = {
    "v1_single_json": 10,
    "v2_sharded_sqlite": 20,
    "patch": 30,
}


def _candidate_basis(rule: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "source_format": rule["source_format"],
        "source_version": rule.get("version", "patch"),
        "source_priority": rule.get("source_priority", SOURCE_PRIORITY.get(rule["source_format"], 0)),
        "generation": rule["generation"],
        "declared_clock_ms": rule["declared_clock_ms"],
        "original_time": rule.get("original_time"),
        "effective_at_ms": rule.get("effective_at_ms"),
    }


def build_unified_view(
    v1_rules: List[Dict[str, Any]],
    v2_rules: List[Dict[str, Any]],
    *,
    injected_clock_ms: int,
    created_by: str = "merge",
    package_version: Optional[int] = None,
) -> Dict[str, Any]:
    clock = LogicalClock(injected_clock_ms)
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for rule in v1_rules + v2_rules:
        by_name.setdefault(rule["name"], []).append(rule)

    unified_rules: List[Dict[str, Any]] = []
    decisions: List[Dict[str, Any]] = []

    for name in sorted(by_name):
        candidates = by_name[name]
        v1_candidate = next((item for item in candidates if item.get("version") == "v1"), None)
        v2_candidate = next((item for item in candidates if item.get("version") == "v2"), None)
        if v1_candidate and v2_candidate and v2_candidate["generation"] < v1_candidate["generation"]:
            raise EngineError(
                ErrorCode.GENERATION_NOT_MONOTONIC,
                "cross-version rule generation must be non-decreasing",
                details={
                    "rule": name,
                    "v1_generation": v1_candidate["generation"],
                    "v2_generation": v2_candidate["generation"],
                },
            )

        ranked = sorted(
            candidates,
            key=lambda item: (
                item["declared_clock_ms"],
                item.get("source_priority", SOURCE_PRIORITY.get(item["source_format"], 0)),
                item["generation"],
            ),
            reverse=True,
        )
        winner = ranked[0]
        runner_up = ranked[1] if len(ranked) > 1 else None
        equal_clock_tie = bool(
            runner_up and winner["declared_clock_ms"] == runner_up["declared_clock_ms"]
        )
        if equal_clock_tie:
            tie_reason = "equal_declared_clock;source_format_priority_desc"
        elif runner_up:
            tie_reason = "declared_logical_clock_desc"
        else:
            tie_reason = "single_source"

        effective_json: Dict[str, Any]
        conversion: Optional[Dict[str, Any]] = None
        if winner["source_format"] == "v1_single_json" or winner.get("version") == "v1":
            conversion = clock.advance_seconds(winner["relative_effective_seconds"])
            effective_at_ms = conversion["effective_at_ms"]
            effective_json = {
                "original": winner["original_time"],
                "absolute_effective_at_ms": effective_at_ms,
                "conversion": conversion,
            }
        else:
            effective_at_ms = winner["effective_at_ms"]
            effective_json = {
                "original": winner["original_time"],
                "absolute_effective_at_ms": effective_at_ms,
                "conversion": None,
            }

        decision = {
            "rule_name": name,
            "selected_source_format": winner["source_format"],
            "selected_source_version": winner.get("version", "patch"),
            "adjudication_policy": [
                "declared_logical_clock_ms descending",
                "if clocks equal: source format priority descending (v2=20, patch=30, v1=10)",
                "generation monotonicity is validated and retained in every candidate basis",
            ],
            "tie_break_applied": tie_reason,
            "candidates": [_candidate_basis(item) for item in ranked],
        }
        rule = {
            "name": name,
            "source_format": winner["source_format"],
            "source_version": winner.get("version", "patch"),
            "source_priority": winner.get("source_priority", SOURCE_PRIORITY.get(winner["source_format"], 0)),
            "generation": winner["generation"],
            "declared_clock_ms": winner["declared_clock_ms"],
            "effective_at_ms": effective_at_ms,
            "effective_time": effective_json,
            "original_time": winner.get("original_time"),
            "conversion": conversion,
            "verify_type": winner.get("verify_type", "status"),
            "dependencies": list(winner.get("dependencies", [])),
            "tombstone": bool(winner.get("tombstone", False)),
            "content_digest": winner.get("content_digest"),
            "decision_basis": decision,
        }
        unified_rules.append(rule)
        decisions.append(decision)

    input_digest = sha256_object(
        {
            "clock_ms": injected_clock_ms,
            "v1": [_digest_rule(rule) for rule in sorted(v1_rules, key=lambda item: item["name"])],
            "v2": [_digest_rule(rule) for rule in sorted(v2_rules, key=lambda item: item["name"])],
        }
    )
    view = {
        "view_uid": None,
        "created_by": created_by,
        "package_version": package_version,
        "injected_clock_ms": injected_clock_ms,
        "input_digest": input_digest,
        "rule_count": len(unified_rules),
        "rules": unified_rules,
        "decisions": decisions,
    }
    view["decision_digest"] = sha256_object(
        [_rule_decision_digest(rule) for rule in unified_rules]
    )
    view["view_uid"] = "view-" + view["decision_digest"].split(":", 1)[1][:16]
    view["summary"] = {
        "view_uid": view["view_uid"],
        "rule_count": len(unified_rules),
        "v1_selected": sum(rule["source_version"] == "v1" for rule in unified_rules),
        "v2_selected": sum(rule["source_version"] == "v2" for rule in unified_rules),
        "patch_selected": sum(rule["source_version"] == "patch" for rule in unified_rules),
        "tombstoned": sum(rule["tombstone"] for rule in unified_rules),
        "decision_digest": view["decision_digest"],
        "input_digest": input_digest,
    }
    return view


def _digest_rule(rule: Dict[str, Any]) -> Dict[str, Any]:
    keys = [
        "name",
        "source_format",
        "version",
        "generation",
        "declared_clock_ms",
        "relative_effective_seconds",
        "effective_at_ms",
        "verify_type",
        "dependencies",
        "tombstone",
        "content_digest",
    ]
    return {key: rule.get(key) for key in keys if key in rule}


def _rule_decision_digest(rule: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": rule["name"],
        "source_format": rule["source_format"],
        "generation": rule["generation"],
        "declared_clock_ms": rule["declared_clock_ms"],
        "effective_at_ms": rule["effective_at_ms"],
        "dependencies": rule["dependencies"],
        "tombstone": rule["tombstone"],
        "decision_basis": rule["decision_basis"],
    }


def render_merge_result(view: Dict[str, Any]) -> str:
    return canonical_text(
        {
            "view_uid": view["view_uid"],
            "input_digest": view["input_digest"],
            "decision_digest": view["decision_digest"],
            "summary": view["summary"],
            "rules": [
                {
                    "name": rule["name"],
                    "source_version": rule["source_version"],
                    "generation": rule["generation"],
                    "declared_clock_ms": rule["declared_clock_ms"],
                    "effective_at_ms": rule["effective_at_ms"],
                    "original_time": rule["original_time"],
                    "conversion": rule["conversion"],
                    "decision_basis": rule["decision_basis"],
                }
                for rule in view["rules"]
            ],
        }
    )
