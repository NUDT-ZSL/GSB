from __future__ import annotations

import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from .canonical import sha256_bytes, sha256_json
from .errors import WorkbenchError
from .ioformats import load_shard_manifest, read_json_file, verify_file_digest

SOURCE_PRIORITY = {"v1": 10, "v2": 20}


def _rule_dict(raw: dict[str, Any], code: str, path: str) -> dict[str, dict[str, Any]]:
    rules = raw.get("rules")
    result: dict[str, dict[str, Any]] = {}
    if isinstance(rules, dict):
        for key, body in rules.items():
            if not isinstance(body, dict):
                raise WorkbenchError(code, "rule body must be an object", {"path": path, "rule_key": key})
            result[key] = {**body, "rule_key": body.get("rule_key", body.get("id", key))}
    elif isinstance(rules, list):
        for index, body in enumerate(rules):
            if not isinstance(body, dict):
                raise WorkbenchError(code, "rule body must be an object", {"path": path, "index": index})
            key = body.get("rule_key", body.get("id", body.get("key")))
            if not isinstance(key, str) or not key:
                raise WorkbenchError(code, "rule_key is required", {"path": path, "index": index})
            if key in result:
                raise WorkbenchError(code, "duplicate rule_key in snapshot", {"path": path, "rule_key": key})
            result[key] = {**body, "rule_key": key}
    else:
        raise WorkbenchError(code, "rules must be an object or array", {"path": path})
    if not result:
        raise WorkbenchError(code, "at least one rule is required", {"path": path})
    return result


def _thresholds(body: dict[str, Any], code: str, key: str) -> dict[str, int]:
    suspect = body.get("suspect_threshold", body.get("suspectThreshold", 1))
    lost = body.get("lost_threshold", body.get("lostThreshold", 3))
    if isinstance(suspect, bool) or not isinstance(suspect, int) or suspect < 1:
        raise WorkbenchError(code, "suspect_threshold must be a positive integer", {"rule_key": key, "value": suspect})
    if isinstance(lost, bool) or not isinstance(lost, int) or lost < suspect:
        raise WorkbenchError(code, "lost_threshold must be an integer >= suspect_threshold", {"rule_key": key, "suspect_threshold": suspect, "lost_threshold": lost})
    return {"suspect_threshold": suspect, "lost_threshold": lost}


def load_v1_rules(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    raw = read_json_file(path)
    code = "E_RULE_V1_INVALID"
    if raw.get("format", "v1") not in {"v1", "health-rules-v1", None}:
        raise WorkbenchError(code, "unsupported v1 format marker", {"path": str(path), "format": raw.get("format")})
    effective_after = raw.get("effective_after_seconds", raw.get("effectiveAfterSeconds", raw.get("relative_effective_seconds")))
    if isinstance(effective_after, bool) or not isinstance(effective_after, int) or effective_after < 0:
        raise WorkbenchError(code, "effective_after_seconds must be a non-negative integer", {"path": str(path), "value": effective_after})
    declared_clock = raw.get("declared_clock_ms", raw.get("logical_clock_ms", raw.get("declaredClockMs", 0)))
    if isinstance(declared_clock, bool) or not isinstance(declared_clock, int) or declared_clock < 0:
        raise WorkbenchError(code, "declared_clock_ms must be a non-negative integer", {"path": str(path), "value": declared_clock})

    rules = _rule_dict(raw, code, str(path))
    instance_rules = raw.get("instance_rules", raw.get("instanceRules", raw.get("flat_instance_rules", {})))
    if not isinstance(instance_rules, dict):
        raise WorkbenchError(code, "instance_rules must be a flat object", {"path": str(path)})

    normalized_rules: dict[str, Any] = {}
    for key, body in rules.items():
        generation = body.get("generation", body.get("rule_generation", 0))
        clock = body.get("declared_clock_ms", body.get("logical_clock_ms", declared_clock))
        rel_seconds = body.get("effective_after_seconds", body.get("relative_effective_seconds", effective_after))
        for field_name, value in (("generation", generation), ("declared_clock_ms", clock), ("effective_after_seconds", rel_seconds)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise WorkbenchError(code, f"{field_name} must be a non-negative integer", {"rule_key": key, "value": value})
        upstream = body.get("upstream_rules", body.get("depends_on", body.get("dependencies", [])))
        if not isinstance(upstream, list) or not all(isinstance(item, str) for item in upstream):
            raise WorkbenchError(code, "upstream_rules must be an array of strings", {"rule_key": key})
        instance_ids = body.get("instance_ids", body.get("instances", []))
        if not isinstance(instance_ids, list) or not all(isinstance(item, str) for item in instance_ids):
            raise WorkbenchError(code, "instance_ids must be an array of strings", {"rule_key": key})
        thresholds = _thresholds(body, code, key)
        normalized_rules[key] = {
            "rule_key": key,
            "source": "v1",
            "source_priority": SOURCE_PRIORITY["v1"],
            "generation": generation,
            "declared_clock_ms": clock,
            "relative_effective_seconds": rel_seconds,
            "tombstone": False,
            "enabled": True,
            "upstream_rules": sorted(set(upstream)),
            "instance_ids": sorted(set(instance_ids)),
            "config": thresholds,
            "raw_time": {"representation": "relative_seconds", "effective_after_seconds": rel_seconds},
        }

    normalized_mapping: dict[str, str] = {}
    for instance_id, target in instance_rules.items():
        rule_key = target.get("rule_key") if isinstance(target, dict) else target
        if not isinstance(rule_key, str) or rule_key not in normalized_rules:
            raise WorkbenchError(
                "E_RULE_V1_MAPPING_INVALID",
                "flat instance_rules mapping must reference a declared rule",
                {"path": str(path), "instance_id": instance_id, "rule_key": rule_key},
            )
        normalized_mapping[instance_id] = rule_key

    for key, rule in normalized_rules.items():
        mapped_instances = sorted(iid for iid, rkey in normalized_mapping.items() if rkey == key)
        declared_instances = rule["instance_ids"]
        if declared_instances and declared_instances != mapped_instances:
            raise WorkbenchError(
                "E_RULE_V1_MAPPING_INVALID",
                "rule-declared instances and flat instance_rules mapping are inconsistent",
                {"path": str(path), "rule_key": key, "declared": declared_instances, "mapped": mapped_instances},
            )
        rule["instance_ids"] = mapped_instances or declared_instances

    return {
        "schema": "health-rules-v1",
        "path": str(path),
        "effective_after_seconds": effective_after,
        "declared_clock_ms": declared_clock,
        "rules": normalized_rules,
        "instance_rules": normalized_mapping,
        "content_hash": sha256_json(raw),
    }


def _validate_v2_generation_history(conn: sqlite3.Connection) -> None:
    try:
        rows = conn.execute(
            "SELECT rule_key, generation FROM rule_generation_history ORDER BY rule_key, ordinal, rowid"
        ).fetchall()
    except sqlite3.DatabaseError as exc:
        raise WorkbenchError("E_RULE_V2_DB_INVALID", f"cannot read rule_generation_history: {exc}") from exc
    by_key: dict[str, list[int]] = {}
    for rule_key, generation in rows:
        if not isinstance(generation, int) or generation < 0:
            raise WorkbenchError("E_RULE_GENERATION_NONMONOTONIC", "rule generation must be non-negative", {"rule_key": rule_key, "generation": generation})
        by_key.setdefault(rule_key, []).append(generation)
    for rule_key, generations in by_key.items():
        for previous, current in zip(generations, generations[1:]):
            if current <= previous:
                raise WorkbenchError(
                    "E_RULE_GENERATION_NONMONOTONIC",
                    "v2 rule generation history must be strictly increasing",
                    {"rule_key": rule_key, "generations": generations},
                )


def load_v2_bundle(manifest_path: str | Path, db_path: str | Path | None = None) -> dict[str, Any]:
    manifest = load_shard_manifest(manifest_path, verify_files=True)
    manifest_raw = read_json_file(manifest["path"])
    manifest_content_hash = sha256_json(manifest_raw)
    db_value = db_path or manifest_raw.get("metadata_db") or manifest_raw.get("sqlite_db")
    if not db_value:
        raise WorkbenchError("E_RULE_V2_INVALID", "v2 manifest does not declare metadata_db", {"manifest": manifest["path"]})
    db_path_obj = Path(db_value)
    if not db_path_obj.is_absolute():
        db_path_obj = Path(manifest["directory"]) / db_path_obj
    if not db_path_obj.is_file():
        raise WorkbenchError("E_RULE_V2_DB_MISSING", "v2 SQLite metadata database is missing", {"db_path": str(db_path_obj)})

    shard_rules: dict[str, dict[str, Any]] = {}
    for shard in manifest["shards"]:
        if not shard["exists"]:
            raise WorkbenchError("E_SHARD_FILE_MISSING", "v2 rule shard is missing", shard)
        raw_shard = read_json_file(shard["path"])
        rules = _rule_dict(raw_shard, "E_RULE_V2_INVALID", shard["path"])
        for key, body in rules.items():
            if key in shard_rules:
                raise WorkbenchError("E_RULE_V2_INVALID", "rule appears in multiple shards", {"rule_key": key})
            shard_rules[key] = body

    try:
        conn = sqlite3.connect(f"file:{db_path_obj}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error as exc:
        raise WorkbenchError("E_RULE_V2_DB_INVALID", str(exc), {"db_path": str(db_path_obj)}) from exc

    try:
        _validate_v2_generation_history(conn)
        try:
            db_shards = {row["shard_id"]: dict(row) for row in conn.execute("SELECT * FROM shards")}
            meta_rows = {row["rule_key"]: dict(row) for row in conn.execute("SELECT * FROM rule_meta")}
        except sqlite3.DatabaseError as exc:
            raise WorkbenchError("E_RULE_V2_DB_INVALID", f"required v2 metadata tables are unreadable: {exc}", {"db_path": str(db_path_obj)}) from exc
    finally:
        conn.close()

    for shard in manifest["shards"]:
        db_shard = db_shards.get(shard["shard_id"])
        if not db_shard:
            raise WorkbenchError("E_RULE_V2_INVALID", "shard missing from SQLite metadata", {"shard_id": shard["shard_id"]})
        if db_shard.get("content_sha256") != shard["content_digest"]:
            raise WorkbenchError(
                "E_SHARD_DIGEST_MISMATCH",
                "SQLite shard hash does not match manifest",
                {"shard_id": shard["shard_id"], "db_digest": db_shard.get("content_sha256"), "manifest_digest": shard["content_digest"]},
            )
        verify_file_digest(Path(shard["path"]), shard["content_digest"])

    rules: dict[str, Any] = {}
    bindings: dict[str, str] = {}
    for key, body in shard_rules.items():
        meta = meta_rows.get(key)
        if not meta:
            raise WorkbenchError("E_RULE_V2_INVALID", "rule missing in SQLite metadata", {"rule_key": key})
        generation = meta["generation"]
        effective_at = meta["effective_at_ms"]
        declared_clock = meta["declared_clock_ms"]
        tombstone = bool(meta["tombstone"])
        upstream = json_loads(meta["upstream_rules_json"], [])
        if not isinstance(generation, int) or generation < 0:
            raise WorkbenchError("E_RULE_GENERATION_NONMONOTONIC", "generation must be non-negative", {"rule_key": key, "generation": generation})
        if not isinstance(effective_at, int) or effective_at < 0:
            raise WorkbenchError("E_RULE_V2_INVALID", "effective_at_ms must be non-negative integer", {"rule_key": key, "value": effective_at})
        if not isinstance(declared_clock, int) or declared_clock < 0:
            raise WorkbenchError("E_RULE_V2_INVALID", "declared_clock_ms must be non-negative integer", {"rule_key": key, "value": declared_clock})
        if upstream != sorted(set(upstream)):
            upstream = sorted(set(upstream))

        json_generation = body.get("generation", body.get("rule_generation", generation))
        json_effective = body.get("effective_at_ms", body.get("absolute_effective_ms", effective_at))
        json_clock = body.get("declared_clock_ms", body.get("logical_clock_ms", declared_clock))
        json_tombstone = bool(body.get("tombstone", body.get("deleted", False)))
        json_upstream = sorted(set(body.get("upstream_rules", body.get("depends_on", []))))
        if (json_generation, json_effective, json_clock, json_tombstone, json_upstream) != (
            generation,
            effective_at,
            declared_clock,
            tombstone,
            upstream,
        ):
            raise WorkbenchError(
                "E_RULE_V2_METADATA_MISMATCH",
                "JSON shard rule metadata does not match SQLite authoritative metadata",
                {
                    "rule_key": key,
                    "json": {"generation": json_generation, "effective_at_ms": json_effective, "declared_clock_ms": json_clock, "tombstone": json_tombstone, "upstream_rules": json_upstream},
                    "sqlite": {"generation": generation, "effective_at_ms": effective_at, "declared_clock_ms": declared_clock, "tombstone": tombstone, "upstream_rules": upstream},
                },
            )

        instance_ids = sorted(set(body.get("instance_ids", body.get("instances", []))))
        thresholds = _thresholds(body, "E_RULE_V2_INVALID", key) if not tombstone else body.get("thresholds", {"suspect_threshold": 1, "lost_threshold": 3})
        if tombstone:
            thresholds = {"suspect_threshold": 1, "lost_threshold": 3}
        rule = {
            "rule_key": key,
            "source": "v2",
            "source_priority": SOURCE_PRIORITY["v2"],
            "generation": generation,
            "declared_clock_ms": declared_clock,
            "effective_at_ms": effective_at,
            "tombstone": tombstone,
            "enabled": not tombstone,
            "tombstone_reason": meta["tombstone_reason"],
            "upstream_rules": upstream,
            "instance_ids": instance_ids,
            "config": thresholds,
            "raw_time": {"representation": "absolute_ms", "effective_at_ms": effective_at},
            "time_conversion": {"representation": "absolute_ms", "scale_ms_per_unit": 1, "rounding_direction": "identity", "rounding_mode": "no_conversion"},
        }
        rules[key] = rule
        for instance_id in instance_ids:
            bindings[instance_id] = key

    for key, meta in meta_rows.items():
        if key not in rules:
            tombstone = bool(meta["tombstone"])
            upstream = json_loads(meta["upstream_rules_json"], [])
            rules[key] = {
                "rule_key": key,
                "source": "v2",
                "source_priority": SOURCE_PRIORITY["v2"],
                "generation": meta["generation"],
                "declared_clock_ms": meta["declared_clock_ms"],
                "effective_at_ms": meta["effective_at_ms"],
                "tombstone": tombstone,
                "enabled": not tombstone,
                "tombstone_reason": meta["tombstone_reason"],
                "upstream_rules": upstream,
                "instance_ids": [],
                "config": {"suspect_threshold": 1, "lost_threshold": 3},
                "raw_time": {"representation": "absolute_ms", "effective_at_ms": meta["effective_at_ms"]},
                "time_conversion": {"representation": "absolute_ms", "scale_ms_per_unit": 1, "rounding_direction": "identity", "rounding_mode": "no_conversion"},
            }

    return {
        "schema": "health-rules-v2-bundle/v1",
        "manifest_path": str(manifest_path),
        "db_path": str(db_path_obj),
        "manifest_content_hash": manifest_content_hash,
        "db_content_hash": sha256_bytes(db_path_obj.read_bytes()),
        "rules": rules,
        "instance_rules": bindings,
    }


def json_loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    import json

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _arbitrate(candidates: list[dict[str, Any]]) -> tuple[dict[str, Any], dict[str, Any]]:
    if len(candidates) == 1:
        winner = candidates[0]
        runner_up = None
        ordered = candidates
    else:
        clock_values = {item["declared_clock_ms"] for item in candidates}
        if len(clock_values) == 1:
            ordered = sorted(candidates, key=lambda rule: rule["source_priority"], reverse=True)
            winner = ordered[0]
            runner_up = ordered[1]
        else:
            ordered = sorted(
                candidates,
                key=lambda rule: (rule["generation"], rule["declared_clock_ms"], rule["source_priority"]),
                reverse=True,
            )
            winner = ordered[0]
            runner_up = ordered[1]
    if runner_up is None:
        reason = f"only_{winner['source']}_candidate"
        basis = "single candidate"
    elif winner["declared_clock_ms"] == runner_up["declared_clock_ms"]:
        reason = "same_logical_clock_source_priority"
        basis = "source_format_priority"
    elif winner["generation"] != runner_up["generation"]:
        reason = "higher_rule_generation"
        basis = "rule_generation"
    else:
        reason = "equal_generation_higher_declared_clock"
        basis = "declared_clock_ms"
    decision = {
        "rule_key": winner["rule_key"],
        "winner_source": winner["source"],
        "winner_generation": winner["generation"],
        "winner_declared_clock_ms": winner["declared_clock_ms"],
        "arbitration_key_order": ["rule_generation", "declared_clock_ms", "source_priority_desc"],
        "decisive_basis": basis,
        "reason": reason,
        "logical_clock_tie": runner_up is not None and winner["declared_clock_ms"] == runner_up["declared_clock_ms"],
        "generation_tie": runner_up is not None and winner["generation"] == runner_up["generation"],
        "source_priority": SOURCE_PRIORITY,
        "candidates": [
            {
                "source": item["source"],
                "generation": item["generation"],
                "declared_clock_ms": item["declared_clock_ms"],
                "source_priority": item["source_priority"],
                "tombstone": item["tombstone"],
                "enabled": item.get("enabled", not item.get("tombstone", False)),
                "effective_at_ms": item.get("effective_at_ms"),
                "raw_time": item.get("raw_time"),
                "time_conversion": item.get("time_conversion"),
                "upstream_rules": item.get("upstream_rules", []),
            }
            for item in ordered
        ],
    }
    return winner, decision


def merge_rule_sources(
    v1: dict[str, Any] | None,
    v2: dict[str, Any] | None,
    *,
    injection_clock_ms: int = 0,
) -> dict[str, Any]:
    if injection_clock_ms < 0:
        raise WorkbenchError("E_CLOCK_INVALID", "injection_clock_ms must be non-negative", {"value": injection_clock_ms})

    candidates_by_key: dict[str, list[dict[str, Any]]] = {}
    binding_candidates: dict[str, list[dict[str, Any]]] = {}

    if v1:
        for key, source_rule in v1["rules"].items():
            rule = deepcopy(source_rule)
            rel = rule.pop("relative_effective_seconds")
            effective_at = injection_clock_ms + rel * 1000
            rule["effective_at_ms"] = effective_at
            rule["time_conversion"] = {
                "source_representation": "v1.relative_effective_after_seconds",
                "injection_clock_ms": injection_clock_ms,
                "raw_value_seconds": rel,
                "scale_ms_per_second": 1000,
                "input_is_non_negative_integer": True,
                "rounding_direction": "none_exact",
                "rounding_mode": "integer_seconds_multiplied_by_1000",
                "absolute_effective_ms": effective_at,
            }
            candidates_by_key.setdefault(key, []).append(rule)
        for instance_id, key in v1["instance_rules"].items():
            rule = v1["rules"][key]
            binding_candidates.setdefault(instance_id, []).append(
                {"instance_id": instance_id, "rule_key": key, "generation": rule["generation"], "declared_clock_ms": rule["declared_clock_ms"], "source_priority": SOURCE_PRIORITY["v1"], "source": "v1"}
            )

    if v2:
        for key, rule in v2["rules"].items():
            candidates_by_key.setdefault(key, []).append(deepcopy(rule))
        for instance_id, key in v2["instance_rules"].items():
            rule = v2["rules"][key]
            binding_candidates.setdefault(instance_id, []).append(
                {"instance_id": instance_id, "rule_key": key, "generation": rule["generation"], "declared_clock_ms": rule["declared_clock_ms"], "source_priority": SOURCE_PRIORITY["v2"], "source": "v2"}
            )

    merged_rules: dict[str, Any] = {}
    decisions: list[dict[str, Any]] = []
    for key in sorted(candidates_by_key):
        winner, decision = _arbitrate(candidates_by_key[key])
        winner["arbitration"] = decision
        merged_rules[key] = winner
        decisions.append(decision)

    instance_bindings: dict[str, Any] = {}
    for instance_id in sorted(binding_candidates):
        candidates = binding_candidates[instance_id]
        ordered = sorted(candidates, key=lambda item: (item["generation"], item["declared_clock_ms"], item["source_priority"]), reverse=True)
        winner = ordered[0]
        instance_bindings[instance_id] = {
            "instance_id": instance_id,
            "rule_key": winner["rule_key"],
            "source": winner["source"],
            "generation": winner["generation"],
            "declared_clock_ms": winner["declared_clock_ms"],
            "decision": {
                "reason": "binding_arbitrated_by_generation_clock_then_source_priority",
                "candidates": ordered,
            },
        }

    view = {
        "schema": "unified-rule-view/v1",
        "injection_clock_ms": injection_clock_ms,
        "rules": merged_rules,
        "instance_bindings": instance_bindings,
        "merge_decisions": decisions,
        "source_hashes": {
            "v1_content_hash": v1.get("content_hash") if v1 else None,
            "v2_manifest_content_hash": v2.get("manifest_content_hash") if v2 else None,
            "v2_db_content_hash": v2.get("db_content_hash") if v2 else None,
        },
        "source_locations": {
            "v1_path": v1.get("path") if v1 else None,
            "v2_manifest_path": v2.get("manifest_path") if v2 else None,
            "v2_db_path": v2.get("db_path") if v2 else None,
        },
    }
    view["content_hash"] = compute_view_hash(view)
    return view


def compute_view_hash(view: dict[str, Any]) -> str:
    return sha256_json(
        {key: value for key, value in view.items() if key not in {"content_hash", "source_locations"}}
    )


def dependency_closure(changed_rule_keys: Iterable[str], rules: dict[str, Any]) -> dict[str, Any]:
    changed = sorted(set(changed_rule_keys))
    reverse_dependencies: dict[str, set[str]] = {key: set() for key in rules}
    for key, rule in rules.items():
        for upstream in rule.get("upstream_rules", []):
            reverse_dependencies.setdefault(upstream, set()).add(key)

    closure: set[str] = set()
    queue = list(changed)
    edges: list[dict[str, str]] = []
    while queue:
        current = queue.pop(0)
        if current in closure:
            continue
        closure.add(current)
        for dependent in sorted(reverse_dependencies.get(current, set())):
            edges.append({"from_upstream": current, "to_dependent": dependent})
            if dependent not in closure:
                queue.append(dependent)

    affected_instances = sorted(
        instance_id
        for instance_id, binding in _binding_items(rules)
        if binding in closure
    )
    return {
        "changed_rule_keys": changed,
        "closure_rule_keys": sorted(closure),
        "reverse_edges": sorted(edges, key=lambda item: (item["from_upstream"], item["to_dependent"])),
        "affected_instances": affected_instances,
    }


def _binding_items(rules: dict[str, Any]) -> Iterable[tuple[str, str]]:
    for rule_key, rule in rules.items():
        for instance_id in rule.get("instance_ids", []):
            yield instance_id, rule_key


def plan_instance_recomputation(
    registry_instances: list[dict[str, Any]],
    view: dict[str, Any],
    closure_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    affected = set(closure_report["affected_instances"]) if closure_report else set()
    plans = []
    for instance in sorted(registry_instances, key=lambda item: item["instance_id"]):
        instance_id = instance["instance_id"]
        binding = view.get("instance_bindings", {}).get(instance_id)
        rule_key = binding["rule_key"] if binding else None
        if closure_report is None:
            action = "recompute"
            reason = "full_reconcile"
        elif instance_id in affected:
            action = "recompute"
            reason = "dependency_invalidation_closure"
        else:
            action = "reuse"
            reason = "outside_dependency_closure"
        plans.append({"instance_id": instance_id, "action": action, "reason": reason, "rule_key": rule_key})
    return {"plans": plans, "recompute": sorted(p["instance_id"] for p in plans if p["action"] == "recompute"), "reuse": sorted(p["instance_id"] for p in plans if p["action"] == "reuse")}


def apply_patch_to_view(view: dict[str, Any], patch: dict[str, Any], *, patch_clock_ms: int) -> tuple[dict[str, Any], dict[str, Any]]:
    next_view = deepcopy(view)
    rules = next_view["rules"]
    changed_keys: set[str] = set()
    change_reports: list[dict[str, Any]] = []

    for change in patch["changes"]:
        op = change["op"]
        key = change["rule_key"]
        changed_keys.add(key)
        rule = rules.get(key)
        if rule is None and op == "upsert_rule":
            body = change.get("rule", {})
            generation = change.get("generation", body.get("generation", 1))
            rule = {
                "rule_key": key,
                "source": "patch",
                "source_priority": 30,
                "generation": generation,
                "declared_clock_ms": change.get("declared_clock_ms", patch_clock_ms),
                "effective_at_ms": change.get("effective_at_ms", patch_clock_ms),
                "tombstone": False,
                "enabled": True,
                "upstream_rules": sorted(set(body.get("upstream_rules", []))),
                "instance_ids": sorted(set(body.get("instance_ids", []))),
                "config": body.get("config", {"suspect_threshold": 1, "lost_threshold": 3}),
                "raw_time": {"representation": "patch_absolute_ms", "effective_at_ms": change.get("effective_at_ms", patch_clock_ms)},
                "time_conversion": {"representation": "absolute_ms", "rounding_direction": "identity"},
            }
            rules[key] = rule
        if rule is None:
            raise WorkbenchError("E_PATCH_TARGET_MISSING", "patch targets an unknown rule_key", {"patch_id": patch["patch_id"], "rule_key": key, "op": op})

        old = deepcopy({k: rule.get(k) for k in ("upstream_rules", "tombstone", "enabled", "effective_at_ms", "generation")})
        if op == "add_dependency":
            upstream = change.get("upstream_rule", change.get("upstream_rules"))
            additions = upstream if isinstance(upstream, list) else [upstream]
            rule["upstream_rules"] = sorted(set(rule.get("upstream_rules", [])) | set(additions))
        elif op == "replace_dependencies":
            upstream = change.get("upstream_rules", [])
            rule["upstream_rules"] = sorted(set(upstream))
        elif op == "tombstone":
            rule["tombstone"] = True
            rule["enabled"] = False
            rule["tombstone_reason"] = change.get("reason", patch.get("reason", "patched tombstone"))
            rule["tombstone_patch_id"] = patch["patch_id"]
            rule["tombstone_effective_at_ms"] = change.get("effective_at_ms", patch_clock_ms)
        elif op == "restore":
            rule["tombstone"] = False
            rule["enabled"] = True
            rule.pop("tombstone_reason", None)
        elif op == "update_effective_at":
            new_time = change.get("effective_at_ms", change.get("new_effective_at_ms"))
            if not isinstance(new_time, int) or new_time < 0:
                raise WorkbenchError("E_PATCH_INVALID", "effective_at_ms must be non-negative integer", {"rule_key": key, "value": new_time})
            rule["effective_at_ms"] = new_time
            rule["time_conversion"] = {"representation": "patch_absolute_ms", "scale_ms_per_unit": 1, "rounding_direction": "identity", "rounding_mode": "no_conversion"}
        elif op == "upsert_rule":
            body = change.get("rule", {})
            new_generation = change.get("generation", body.get("generation", rule["generation"] + 1))
            if new_generation < rule["generation"]:
                raise WorkbenchError("E_RULE_GENERATION_NONMONOTONIC", "patch generation cannot decrease", {"rule_key": key, "old": rule["generation"], "new": new_generation})
            rule["generation"] = new_generation
            if "upstream_rules" in body:
                rule["upstream_rules"] = sorted(set(body["upstream_rules"]))
            if "config" in body:
                rule["config"] = body["config"]
            if "effective_at_ms" in change:
                rule["effective_at_ms"] = change["effective_at_ms"]

        if op in {"add_dependency", "replace_dependencies", "tombstone", "restore", "update_effective_at"}:
            requested_generation = change.get("new_generation", change.get("generation"))
            next_generation = requested_generation if isinstance(requested_generation, int) else old["generation"] + 1
            if isinstance(next_generation, bool) or next_generation <= old["generation"]:
                raise WorkbenchError(
                    "E_RULE_GENERATION_NONMONOTONIC",
                    "patch must advance rule generation",
                    {"rule_key": key, "current_generation": old["generation"], "requested_generation": next_generation},
                )
            rule["generation"] = next_generation

        new = {k: rule.get(k) for k in old}
        change_reports.append({"op": op, "rule_key": key, "old": old, "new": new})

    for instance_id, binding in list(next_view.get("instance_bindings", {}).items()):
        if binding["rule_key"] in changed_keys:
            binding["generation"] = rules[binding["rule_key"]]["generation"]

    closure_report = dependency_closure(changed_keys, rules)
    next_view["content_hash"] = compute_view_hash(next_view)
    report = {
        "patch_id": patch["patch_id"],
        "patch_content_hash": patch["content_hash"],
        "patch_clock_ms": patch_clock_ms,
        "changes": change_reports,
        "closure": closure_report,
    }
    return next_view, report
