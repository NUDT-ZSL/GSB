from __future__ import annotations

import copy
from collections import deque
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .errors import EngineError, ErrorCode
from .hashing import sha256_object
from .parser import read_json


VALID_OPS = {"upsert", "add", "modify", "update", "delete", "remove", "tombstone", "invalidate"}


def normalize_patch_rule(raw: Dict[str, Any], name: str, clock_ms: int) -> Dict[str, Any]:
    generation = raw.get("generation", raw.get("cache_generation", 1))
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 1:
        raise EngineError(ErrorCode.PATCH_INVALID, "patch rule generation must be a positive integer", details={"name": name})
    declared_clock = raw.get("declared_clock_ms", raw.get("logical_clock_ms", clock_ms))
    if not isinstance(declared_clock, int) or isinstance(declared_clock, bool) or declared_clock < 0:
        raise EngineError(ErrorCode.PATCH_INVALID, "patch rule clock must be a non-negative integer", details={"name": name})
    effective_at_ms = raw.get("effective_at_ms", raw.get("absolute_effective_ms", declared_clock))
    if not isinstance(effective_at_ms, int) or isinstance(effective_at_ms, bool) or effective_at_ms < 0:
        raise EngineError(ErrorCode.INVALID_EFFECTIVE_MS, "patch effective_at_ms must be an integer", details={"name": name})
    dependencies = raw.get("dependencies", raw.get("upstream_rules", raw.get("depends_on", [])))
    if not isinstance(dependencies, list):
        raise EngineError(ErrorCode.PATCH_INVALID, "patch dependencies must be a list", details={"name": name})
    return {
        "name": name,
        "version": "patch",
        "source_format": "patch",
        "source_priority": 30,
        "generation": generation,
        "declared_clock_ms": declared_clock,
        "effective_at_ms": effective_at_ms,
        "original_time": {"type": "absolute_effective_at_ms", "value": effective_at_ms},
        "verify_type": raw.get("verify_type", raw.get("origin_verify_type", "status")),
        "dependencies": sorted({str(item) for item in dependencies}),
        "tombstone": bool(raw.get("tombstone", False)),
        "content_digest": raw.get("content_digest"),
        "relative_effective_seconds": raw.get("relative_effective_seconds", 0),
    }


def parse_patch_file(path: str | Path, *, default_clock: Optional[int] = None) -> Dict[str, Any]:
    payload = read_json(path)
    if not isinstance(payload, dict):
        raise EngineError(ErrorCode.PATCH_INVALID, "patch document must be an object")
    patch_id = payload.get("patch_id", payload.get("id"))
    if not isinstance(patch_id, str) or not patch_id.strip():
        raise EngineError(ErrorCode.PATCH_INVALID, "patch_id is required")
    clock_ms = payload.get("submitted_clock_ms", payload.get("clock_ms", default_clock))
    if not isinstance(clock_ms, int) or isinstance(clock_ms, bool) or clock_ms < 0:
        raise EngineError(ErrorCode.PATCH_INVALID, "submitted_clock_ms must be a non-negative integer")
    raw_changes = payload.get("changes", payload.get("operations", []))
    if not isinstance(raw_changes, list) or not raw_changes:
        raise EngineError(ErrorCode.PATCH_INVALID, "patch must contain a non-empty changes list")

    changes: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in raw_changes:
        if not isinstance(raw, dict):
            raise EngineError(ErrorCode.PATCH_INVALID, "each patch change must be an object")
        op = str(raw.get("op", raw.get("operation", "upsert"))).strip().lower()
        aliases = {"add": "upsert", "modify": "upsert", "update": "upsert", "remove": "delete", "invalidate": "tombstone"}
        op = aliases.get(op, op)
        if op not in {"upsert", "delete", "tombstone"}:
            raise EngineError(ErrorCode.PATCH_INVALID, f"unsupported patch op: {op}")
        name = raw.get("name", raw.get("rule_name"))
        if not isinstance(name, str) or not name.strip():
            raise EngineError(ErrorCode.PATCH_INVALID, "patch change name is required")
        name = name.strip()
        if name in seen:
            raise EngineError(ErrorCode.PATCH_INVALID, "patch contains duplicate change for rule", details={"name": name})
        seen.add(name)
        change = {"op": op, "name": name}
        if op == "upsert":
            raw_rule = raw.get("rule", raw)
            if not isinstance(raw_rule, dict):
                raise EngineError(ErrorCode.PATCH_INVALID, "upsert change must contain a rule object", details={"name": name})
            change["rule"] = normalize_patch_rule(raw_rule, name, clock_ms)
        elif op == "tombstone":
            change["reason"] = raw.get("reason", "patch tombstone")
        changes.append(change)

    patch = {
        "patch_id": patch_id.strip(),
        "submitted_clock_ms": clock_ms,
        "changes": changes,
        "payload": payload,
        "payload_hash": sha256_object(payload),
    }
    return patch


def rules_to_map(rules: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    return {rule["name"]: copy.deepcopy(rule) for rule in rules}


def apply_patch_to_rules(
    rules: List[Dict[str, Any]], patch: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    result = rules_to_map(rules)
    for change in patch["changes"]:
        name = change["name"]
        if change["op"] == "upsert":
            result[name] = copy.deepcopy(change["rule"])
        elif change["op"] == "delete":
            result.pop(name, None)
        elif change["op"] == "tombstone":
            if name in result:
                result[name]["tombstone"] = True
            else:
                result[name] = {
                    "name": name,
                    "source_format": "patch",
                    "source_version": "patch",
                    "generation": 1,
                    "declared_clock_ms": patch["submitted_clock_ms"],
                    "effective_at_ms": patch["submitted_clock_ms"],
                    "dependencies": [],
                    "tombstone": True,
                    "verify_type": "status",
                    "content_digest": None,
                }
    return result


def build_reverse_graph(rule_map: Dict[str, Dict[str, Any]]) -> Dict[str, List[str]]:
    downstream: Dict[str, Set[str]] = {}
    for rule in rule_map.values():
        for dependency in rule.get("dependencies", []):
            downstream.setdefault(dependency, set()).add(rule["name"])
            downstream.setdefault(rule["name"], set())
    for name in rule_map:
        downstream.setdefault(name, set())
    return {key: sorted(values) for key, values in sorted(downstream.items())}


def _union_graph(
    old_rules: List[Dict[str, Any]], new_rule_map: Dict[str, Dict[str, Any]]
) -> Dict[str, List[str]]:
    union_map = rules_to_map(old_rules)
    for name, rule in new_rule_map.items():
        union_map[name] = rule
    # Deleted rules must remain as graph nodes long enough to traverse old edges.
    return build_reverse_graph(union_map)


def compute_closure(
    old_rules: List[Dict[str, Any]],
    patch: Dict[str, Any],
) -> Dict[str, Any]:
    new_rule_map = apply_patch_to_rules(old_rules, patch)
    graph = _union_graph(old_rules, new_rule_map)
    old_map = rules_to_map(old_rules)

    entries: List[Dict[str, Any]] = []
    affected_names: Set[str] = set()

    for change in patch["changes"]:
        trigger = change["name"]
        queue: deque[Tuple[str, List[str]]] = deque([(trigger, [trigger])])
        visited: Set[str] = set()
        while queue:
            current, path = queue.popleft()
            if current in visited:
                continue
            visited.add(current)
            affected_names.add(current)
            exists_after = current in new_rule_map
            is_trigger = current == trigger
            if change["op"] == "delete" and is_trigger:
                reuse = "discard_deleted_rule"
                recompute = False
                impact = "deleted_trigger"
            elif not exists_after:
                reuse = "discard_deleted_rule"
                recompute = False
                impact = "deleted_dependency"
            else:
                missing_dependencies = [
                    dep for dep in new_rule_map[current].get("dependencies", []) if dep not in new_rule_map
                ]
                reuse = "recompute"
                recompute = True
                impact = "trigger" if is_trigger else "downstream_dependency"
                if missing_dependencies:
                    impact = "dependency_broken"
            entries.append(
                {
                    "patch_id": patch["patch_id"],
                    "trigger_patch": {
                        "patch_id": patch["patch_id"],
                        "op": change["op"],
                        "submitted_clock_ms": patch["submitted_clock_ms"],
                    },
                    "trigger_rule": trigger,
                    "rule_name": current,
                    "impact": impact,
                    "dependency_path": path,
                    "path_length": len(path) - 1,
                    "recompute_required": recompute,
                    "conclusion_reuse": reuse,
                    "exists_after_patch": exists_after,
                    "rule_source_after": new_rule_map.get(current, {}).get("source_format"),
                }
            )
            for dependent in graph.get(current, []):
                if dependent not in visited:
                    queue.append((dependent, path + [dependent]))

    entries.sort(key=lambda item: (item["rule_name"], item["trigger_rule"], item["dependency_path"]))
    affected = {entry["rule_name"] for entry in entries}
    reusable_rules = sorted(name for name in old_map if name not in affected)
    result = {
        "closure_id": "closure-" + sha256_object(
            {"patch_id": patch["patch_id"], "changes": patch["changes"], "entries": entries}
        ).split(":", 1)[1][:16],
        "patch_id": patch["patch_id"],
        "trigger_patch": {
            "patch_id": patch["patch_id"],
            "submitted_clock_ms": patch["submitted_clock_ms"],
            "changes": patch["changes"],
        },
        "affected_rules": sorted(affected_names),
        "recompute_rules": sorted(
            entry["rule_name"] for entry in entries if entry["conclusion_reuse"] == "recompute"
        ),
        "discard_rules": sorted(
            entry["rule_name"] for entry in entries if entry["conclusion_reuse"] == "discard_deleted_rule"
        ),
        "reusable_rules": reusable_rules,
        "entries": entries,
        "conclusion_plan": {
            "stale_conclusion_ids": [],
            "stale_windows": [],
            "stale_observation_seqs": [],
            "reusable_conclusion_ids": [],
        },
    }
    result["result_digest"] = sha256_object(result)
    return result
