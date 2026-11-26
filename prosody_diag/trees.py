from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .canonical import canonical_hash, read_json, write_json_atomic
from .config import Config
from .errors import (
    E_FORMAT_CORRUPT,
    E_SEMANTIC_LOSS,
    E_VERSION_UNSUPPORTED,
    DiagnosticError,
)
from .precision import PrecisionContext


SUPPORTED_VERSIONS = {"1.0", "2.0", 1, 2, "1", "2"}


def _corrupt(message: str, field: str | None = None, **details: Any) -> DiagnosticError:
    return DiagnosticError(E_FORMAT_CORRUPT, message, field=field, details=details)


def _major_version(version: Any) -> int:
    if isinstance(version, int):
        return version
    if isinstance(version, float):
        return int(version)
    text = str(version).strip().lower().lstrip("v")
    try:
        return int(text.split(".", 1)[0])
    except (ValueError, IndexError) as exc:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "decision tree version header is unsupported",
            field="version",
            details={"version": version},
        ) from exc


def _rule_number(rule_id: Any) -> int:
    text = str(rule_id)
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        raise _corrupt("rule_id must contain a numeric rule number", field="rule_id", rule_id=rule_id)
    return int(digits)


def _condition_from_mapping(obj: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise _corrupt("condition must be an object", field=field_name, value=obj)
    for key in ("feature", "op", "threshold"):
        if key not in obj:
            raise _corrupt(f"condition is missing {key}", field=f"{field_name}.{key}", condition=obj)
    if obj["op"] not in {">=", ">", "<=", "<", "=="}:
        raise _corrupt("unsupported condition operator", field=f"{field_name}.op", condition=obj)
    try:
        threshold = float(obj["threshold"])
    except (TypeError, ValueError) as exc:
        raise _corrupt("condition threshold must be numeric", field=f"{field_name}.threshold") from exc
    return {"feature": str(obj["feature"]), "op": obj["op"], "threshold": threshold}


def flatten_condition_tree(node: Any) -> list[list[dict[str, Any]]]:
    """Flatten AND/OR trees into disjunctive normal form root-to-leaf paths."""

    if not isinstance(node, dict):
        raise _corrupt("nested rule node must be an object", field="rules.when", value=node)
    if "feature" in node:
        return [[_condition_from_mapping(node, "rules.when")]]
    if "all_of" in node:
        children = node["all_of"]
        if not isinstance(children, list) or not children:
            raise _corrupt("all_of must be a non-empty list", field="rules.when.all_of")
        paths: list[list[dict[str, Any]]] = [[]]
        for child in children:
            child_paths = flatten_condition_tree(child)
            paths = [base + extra for base in paths for extra in child_paths]
        return paths
    if "any_of" in node:
        children = node["any_of"]
        if not isinstance(children, list) or not children:
            raise _corrupt("any_of must be a non-empty list", field="rules.when.any_of")
        paths = []
        for child in children:
            paths.extend(flatten_condition_tree(child))
        return paths
    if "not" in node:
        raise DiagnosticError(
            E_SEMANTIC_LOSS,
            "negated nested condition cannot be flattened without semantic review",
            field="rules.when.not",
            details={"node": node},
        )
    raise _corrupt("nested rule node has no feature/all_of/any_of", field="rules.when", node=node)


def parse_v1_tree(obj: dict[str, Any], source: str | Path | None = None) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise _corrupt("v1 tree must be a JSON object", field="tree")
    if "rule_lines" not in obj:
        raise _corrupt("v1 tree is missing rule_lines", field="rule_lines")
    if "tree_id" not in obj:
        raise _corrupt("v1 tree is missing tree_id", field="tree_id")
    lines = obj["rule_lines"]
    if not isinstance(lines, list):
        raise _corrupt("rule_lines must be a list", field="rule_lines")

    rules: list[dict[str, Any]] = []
    previous_time: float | None = None
    for line_index, line in enumerate(lines):
        field_name = f"rule_lines[{line_index}]"
        if not isinstance(line, str):
            raise _corrupt("rule line must be a pipe-delimited string", field=field_name)
        parts = line.split("|")
        if len(parts) != 7:
            raise _corrupt(
                "v1 rule line must contain exactly seven pipe fields",
                field=field_name,
                expected_fields=7,
                actual_fields=len(parts),
                line=line,
            )
        rule_id, time_sec_text, feature, op, threshold_text, label, confidence_text = parts
        try:
            time_sec = float(time_sec_text)
            threshold = float(threshold_text)
            confidence = float(confidence_text)
        except ValueError as exc:
            raise _corrupt("numeric rule field could not be parsed", field=field_name, line=line) from exc
        if time_sec < 0:
            raise _corrupt("relative timestamp must be non-negative", field=f"{field_name}.time_sec")
        if previous_time is not None and time_sec + 1e-12 < previous_time:
            raise _corrupt(
                "rule timestamp regressed",
                field=f"{field_name}.time_sec",
                current=time_sec,
                previous=previous_time,
            )
        previous_time = time_sec
        rules.append(
            {
                "rule_id": rule_id,
                "rule_number": _rule_number(rule_id),
                "generation": int(obj.get("default_generation", 1)),
                "time_ms": int(round(time_sec * 1000.0)),
                "conditions": [{"feature": feature, "op": op, "threshold": threshold}],
                "label": label,
                "confidence": confidence,
                "weight": 1.0,
                "tombstone": None,
                "active": True,
                "source": "v1",
            }
        )
    rules.sort(key=lambda rule: rule["rule_number"])
    return {
        "tree_id": str(obj["tree_id"]),
        "source_version": "v1",
        "version_header": str(obj.get("version", "1.0")),
        "tree_rev": int(obj.get("tree_rev", 1)),
        "rule_generation": int(obj.get("rule_generation", obj.get("default_generation", 1))),
        "default_label": str(obj.get("default_label", "B0")),
        "default_confidence": float(obj.get("default_confidence", 0.5)),
        "rules": rules,
        "tombstones": [],
        "source_path": str(source) if source else None,
    }


def _open_sqlite_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise _corrupt("v2 SQLite metadata database is missing", field="sqlite", path=str(path))
    try:
        connection = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection
    except sqlite3.Error as exc:
        raise _corrupt("cannot open v2 SQLite metadata database", field="sqlite", error=str(exc)) from exc


def parse_v2_package(root: str | Path) -> dict[str, Any]:
    root_path = Path(root)
    manifest_path = root_path / "manifest.json"
    if not manifest_path.exists():
        raise _corrupt("v2 manifest.json is missing", field="manifest.json")
    manifest = read_json(manifest_path)
    for key in ("format", "version", "shards", "sqlite"):
        if key not in manifest:
            raise _corrupt(f"v2 manifest is missing {key}", field=f"manifest.json.{key}")
    if _major_version(manifest["version"]) != 2:
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "not a v2 decision tree package",
            field="manifest.json.version",
            details={"version": manifest["version"]},
        )

    db_path = root_path / manifest["sqlite"]
    connection = _open_sqlite_readonly(db_path)
    rules: list[dict[str, Any]] = []
    tombstones: list[dict[str, Any]] = []
    previous_first_seen: int | None = None
    try:
        meta_rows = {
            row["rule_id"]: dict(row)
            for row in connection.execute(
                "SELECT rule_id, generation, active, tombstone, tombstone_reason, "
                "first_seen_ms, updated_ms FROM rule_meta"
            )
        }
        tree_meta = {
            row[0]: row[1] for row in connection.execute("SELECT key, value FROM tree_meta")
        }
    except sqlite3.Error as exc:
        raise _corrupt("v2 SQLite metadata schema is invalid", field="sqlite.rule_meta", error=str(exc)) from exc
    finally:
        connection.close()

    for shard_name in manifest["shards"]:
        shard_path = root_path / "shards" / shard_name
        if not shard_path.exists():
            shard_path = root_path / shard_name
        if not shard_path.exists():
            raise _corrupt("v2 shard file is missing", field="shards", path=str(shard_path))
        shard = read_json(shard_path)
        if "rules" not in shard or not isinstance(shard["rules"], list):
            raise _corrupt("v2 shard is missing rules list", field=f"{shard_path.name}.rules")
        for raw_rule in shard["rules"]:
            if not isinstance(raw_rule, dict) or "rule_id" not in raw_rule:
                raise _corrupt("v2 rule is missing rule_id", field="shards.rules.rule_id")
            rule_id = str(raw_rule["rule_id"])
            if "time_ms" not in raw_rule:
                raise _corrupt("v2 rule is missing time_ms", field=f"rules.{rule_id}.time_ms")
            try:
                time_ms = int(raw_rule["time_ms"])
            except (TypeError, ValueError) as exc:
                raise _corrupt("v2 time_ms must be integer milliseconds", field=f"rules.{rule_id}.time_ms") from exc
            if time_ms < 0:
                raise _corrupt("absolute timestamp must be non-negative", field=f"rules.{rule_id}.time_ms")
            meta = meta_rows.get(rule_id)
            if meta is None:
                raise _corrupt("v2 rule has no SQLite metadata row", field=f"rule_meta.{rule_id}")
            first_seen = int(meta["first_seen_ms"])
            updated = int(meta["updated_ms"])
            if updated < first_seen:
                raise _corrupt(
                    "rule metadata timestamp regressed",
                    field=f"rule_meta.{rule_id}.updated_ms",
                    first_seen_ms=first_seen,
                    updated_ms=updated,
                )
            if previous_first_seen is not None and first_seen < previous_first_seen:
                raise _corrupt(
                    "rule first_seen_ms sequence regressed",
                    field=f"rule_meta.{rule_id}.first_seen_ms",
                    first_seen_ms=first_seen,
                    previous_first_seen_ms=previous_first_seen,
                )
            previous_first_seen = first_seen

            condition_node: Any
            if "when" in raw_rule:
                condition_node = raw_rule["when"]
            elif "all_of" in raw_rule:
                condition_node = {"all_of": raw_rule["all_of"]}
            elif "feature" in raw_rule:
                condition_node = raw_rule
            else:
                raise _corrupt(
                    "v2 rule is missing conditions/when/all_of",
                    field=f"rules.{rule_id}.when",
                )
            paths = flatten_condition_tree(condition_node)
            is_tombstone = bool(int(meta.get("tombstone", 0) or 0))
            base_rule = {
                "generation": int(meta["generation"]),
                "time_ms": time_ms,
                "label": str(raw_rule.get("label", "B0")),
                "confidence": float(raw_rule.get("confidence", 0.5)),
                "weight": float(raw_rule.get("weight", 1.0)),
                "tombstone": (
                    {
                        "tombstoned": True,
                        "reason": meta.get("tombstone_reason"),
                        "at_ms": updated,
                    }
                    if is_tombstone
                    else None
                ),
                "active": bool(int(meta.get("active", 1))) and not is_tombstone,
                "source": "v2",
            }
            if is_tombstone:
                tombstones.append({"rule_id": rule_id, **base_rule["tombstone"]})
            if len(paths) == 1:
                rules.append(
                    {
                        "rule_id": rule_id,
                        "rule_number": _rule_number(rule_id),
                        "conditions": paths[0],
                        **base_rule,
                    }
                )
            else:
                for path_index, path in enumerate(paths):
                    flattened_id = f"{rule_id}#F{path_index}"
                    rules.append(
                        {
                            "rule_id": flattened_id,
                            "rule_number": _rule_number(rule_id) * 1000 + path_index,
                            "conditions": path,
                            "nested_source_rule_id": rule_id,
                            **base_rule,
                        }
                    )

    rules.sort(key=lambda rule: (rule["rule_number"], rule["rule_id"]))
    return {
        "tree_id": str(manifest.get("tree_id", root_path.name)),
        "source_version": "v2",
        "version_header": str(manifest["version"]),
        "tree_rev": int(tree_meta.get("tree_rev", manifest.get("tree_rev", 2))),
        "rule_generation": int(tree_meta.get("rule_generation", manifest.get("rule_generation", 2))),
        "default_label": str(manifest.get("default_label", "B0")),
        "default_confidence": float(manifest.get("default_confidence", 0.5)),
        "rules": rules,
        "tombstones": tombstones,
        "source_path": str(root_path),
        "sqlite_meta": tree_meta,
    }


def load_decision_tree(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    if path.is_dir():
        return parse_v2_package(path)
    obj = read_json(path)
    if not isinstance(obj, dict):
        raise DiagnosticError(
            E_VERSION_UNSUPPORTED,
            "decision tree snapshot must be a JSON object",
            field="tree",
            details={"path": str(path)},
        )
    version = obj.get("version") or obj.get("format_version")
    if version is None:
        if "rule_lines" in obj:
            version = "1.0"
        elif "shards" in obj:
            version = "2.0"
        else:
            raise DiagnosticError(
                E_VERSION_UNSUPPORTED,
                "decision tree has no version header or recognizable field set",
                field="version",
            )
    major = _major_version(version)
    if major == 1:
        return parse_v1_tree(obj, path)
    if major == 2:
        if "shards" in obj and not path.parent.joinpath(obj["shards"][0]).exists():
            package_dir = path.parent
        elif "shards" in obj:
            return parse_v2_package(path.parent)
        else:
            package_dir = path.parent
        return parse_v2_package(package_dir)
    raise DiagnosticError(
        E_VERSION_UNSUPPORTED,
        "unsupported decision tree major version",
        field="version",
        details={"version": version},
    )


def canonical_tree_view(tree: dict[str, Any]) -> dict[str, Any]:
    rules = []
    for rule in tree.get("rules", []):
        rules.append(
            {
                "rule_id": rule["rule_id"],
                "rule_number": rule["rule_number"],
                "generation": rule.get("generation", 1),
                "time_ms": rule["time_ms"],
                "conditions": sorted(
                    rule["conditions"],
                    key=lambda item: (item["feature"], item["op"], item["threshold"]),
                ),
                "label": rule["label"],
                "confidence": rule["confidence"],
                "weight": rule.get("weight", 1.0),
                "tombstone": rule.get("tombstone"),
                "active": rule.get("active", True),
            }
        )
    rules.sort(key=lambda item: (item["rule_number"], item["rule_id"]))
    return {
        "tree_id": tree["tree_id"],
        "default_label": tree["default_label"],
        "default_confidence": tree["default_confidence"],
        "rules": rules,
    }


def tree_hash(tree: dict[str, Any]) -> str:
    return canonical_hash(canonical_tree_view(tree))


def write_v2_package(
    root: str | Path,
    manifest: dict[str, Any],
    shards: list[dict[str, Any]],
    rule_meta: Iterable[dict[str, Any]],
    tree_meta: dict[str, str] | None = None,
) -> Path:
    root_path = Path(root)
    shard_dir = root_path / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)
    db_path = root_path / manifest["sqlite"]
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        db_path.unlink()
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "CREATE TABLE tree_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE rule_meta ("
            "rule_id TEXT PRIMARY KEY, generation INTEGER NOT NULL, active INTEGER NOT NULL, "
            "tombstone INTEGER NOT NULL, tombstone_reason TEXT, first_seen_ms INTEGER NOT NULL, "
            "updated_ms INTEGER NOT NULL)"
        )
        if tree_meta:
            connection.executemany(
                "INSERT INTO tree_meta(key, value) VALUES(?, ?)",
                [(key, str(value)) for key, value in tree_meta.items()],
            )
        connection.executemany(
            "INSERT INTO rule_meta(rule_id, generation, active, tombstone, tombstone_reason, "
            "first_seen_ms, updated_ms) VALUES (:rule_id, :generation, :active, :tombstone, "
            ":tombstone_reason, :first_seen_ms, :updated_ms)",
            list(rule_meta),
        )
        connection.commit()
    finally:
        connection.close()
    for shard in shards:
        write_json_atomic(shard_dir / shard["shard_file"], shard["payload"])
    write_json_atomic(root_path / "manifest.json", manifest)
    return root_path


def write_builtin_trees(sample_dir: str | Path, cfg: Config | None = None) -> dict[str, Path]:
    del cfg
    root = Path(sample_dir) / "trees"
    v1_path = root / "v1" / "prosody_tree_v1.json"
    v1 = {
        "format": "prosody-decision-tree",
        "version": "1.0",
        "tree_id": "prosody-mandarin-v1-flat",
        "time_base": "relative_seconds",
        "tree_rev": 1,
        "default_label": "B0",
        "default_confidence": 0.52,
        "rule_lines": [
            "R001|0.0|pause_norm|>=|0.62|B3|0.95",
            "R002|0.5|pause_norm|>=|0.34|B2|0.88",
            "R003|1.0|pause_norm|>=|0.16|B1|0.78",
            "R004|1.5|reset_norm|>=|0.55|B1|0.67",
            "R005|2.0|zcr_norm|>=|0.22|B2|0.64",
        ],
    }
    write_json_atomic(v1_path, v1)

    v2_root = root / "v2"
    v2_rules = [
        {
            "rule_id": "R001",
            "time_ms": 0,
            "all_of": [{"feature": "pause_norm", "op": ">=", "threshold": 0.60}],
            "label": "B3",
            "confidence": 0.96,
            "weight": 1.0,
        },
        {
            "rule_id": "R002",
            "time_ms": 500,
            "all_of": [{"feature": "pause_norm", "op": ">=", "threshold": 0.32}],
            "label": "B2",
            "confidence": 0.90,
            "weight": 1.0,
        },
        {
            "rule_id": "R003",
            "time_ms": 1000,
            "all_of": [{"feature": "pause_norm", "op": ">=", "threshold": 0.22}],
            "label": "B1",
            "confidence": 0.80,
            "weight": 1.0,
        },
        {
            "rule_id": "R004",
            "time_ms": 1500,
            "all_of": [{"feature": "reset_norm", "op": ">=", "threshold": 0.62}],
            "label": "B1",
            "confidence": 0.70,
            "weight": 1.0,
        },
        {
            "rule_id": "R005",
            "time_ms": 2000,
            "all_of": [{"feature": "zcr_norm", "op": ">=", "threshold": 0.20}],
            "label": "B2",
            "confidence": 0.66,
            "weight": 1.0,
        },
        {
            "rule_id": "R006",
            "time_ms": 2200,
            "all_of": [{"feature": "f0_jump_norm", "op": ">=", "threshold": 0.75}],
            "label": "B1",
            "confidence": 0.62,
            "weight": 1.0,
        },
        {
            "rule_id": "R007",
            "time_ms": 2300,
            "all_of": [{"feature": "energy_norm", "op": ">", "threshold": 2.0}],
            "label": "B3",
            "confidence": 0.41,
            "weight": 1.0,
        },
    ]
    active_rules = v2_rules[:6]
    shard0 = {"shard": "shard-000", "rules": active_rules[:3]}
    shard1 = {"shard": "shard-001", "rules": active_rules[3:]}
    # Keep the tombstoned rule in a third shard to exercise SQLite tombstones.
    shard2 = {"shard": "shard-002", "rules": [v2_rules[6]]}
    manifest = {
        "format": "prosody-decision-tree-shards",
        "version": "2.0",
        "tree_id": "prosody-mandarin-v2-sharded",
        "tree_rev": 2,
        "rule_generation": 2,
        "default_label": "B0",
        "default_confidence": 0.52,
        "shards": ["shard-000.json", "shard-001.json", "shard-002.json"],
        "sqlite": "meta/prosody.db",
    }
    meta = []
    for index, rule in enumerate(v2_rules, 1):
        tombstone = rule["rule_id"] == "R007"
        meta.append(
            {
                "rule_id": rule["rule_id"],
                "generation": 1 if tombstone else 2,
                "active": 0 if tombstone else 1,
                "tombstone": 1 if tombstone else 0,
                "tombstone_reason": "superseded by normalized reset rule" if tombstone else None,
                "first_seen_ms": rule["time_ms"],
                "updated_ms": 2500 if tombstone else rule["time_ms"] + 20,
            }
        )
    write_v2_package(
        v2_root,
        manifest,
        [
            {"shard_file": "shard-000.json", "payload": shard0},
            {"shard_file": "shard-001.json", "payload": shard1},
            {"shard_file": "shard-002.json", "payload": shard2},
        ],
        meta,
        tree_meta={"tree_rev": "2", "rule_generation": "2", "format": "2.0"},
    )
    return {"v1": v1_path, "v2": v2_root}


def normalize_candidate_features(
    candidate: dict[str, Any], context: PrecisionContext, cfg: Config
) -> tuple[dict[str, Any], dict[str, float]]:
    raw_specs = {
        "pause_norm": (float(candidate.get("pause_ms", 0)), cfg.pause_scale_ms),
        "energy_norm": (float(candidate.get("energy_rms", 0.0)), cfg.energy_scale),
        "zcr_norm": (float(candidate.get("zcr", 0.0)), cfg.zcr_scale),
        "f0_norm": (float(candidate.get("f0_candidate_hz", 0.0)), cfg.f0_scale_hz),
        "reset_norm": (float(candidate.get("reset_norm_raw", 0.0)), 1.0),
        "f0_jump_norm": (float(candidate.get("f0_jump_norm_raw", 0.0)), 1.0),
    }
    internal: dict[str, Any] = {}
    floats: dict[str, float] = {}
    for name, (numerator, denominator) in raw_specs.items():
        left = context.put(numerator)
        right = context.put(denominator)
        value = context.div(left, right) if denominator != 1.0 else context.put(numerator)
        internal[name] = value
        floats[name] = context.to_float(value)
    return internal, floats


def infer_candidate(
    tree: dict[str, Any],
    candidate: dict[str, Any],
    context: PrecisionContext,
    cfg: Config,
) -> dict[str, Any]:
    internal_features, feature_floats = normalize_candidate_features(candidate, context, cfg)
    path: list[dict[str, Any]] = []
    matches: list[dict[str, Any]] = []
    supports: dict[str, Any] = {}

    for rule in sorted(tree["rules"], key=lambda item: (item["rule_number"], item["rule_id"])):
        if not rule.get("active", True) or rule.get("tombstone"):
            continue
        condition_traces: list[dict[str, Any]] = []
        rule_matched = True
        for condition in rule["conditions"]:
            feature_name = condition["feature"]
            if feature_name not in internal_features:
                raise _corrupt(
                    "rule references an unknown feature",
                    field=f"rules.{rule['rule_id']}.feature",
                    feature=feature_name,
                )
            lhs = internal_features[feature_name]
            rhs = context.put(condition["threshold"])
            margin_internal = context.sub(lhs, rhs)
            op = condition["op"]
            if op == ">=":
                result = context.ge(lhs, rhs)
            elif op == ">":
                result = context.gt(lhs, rhs)
            elif op == "<=":
                result = context.le(lhs, rhs)
            elif op == "<":
                result = context.lt(lhs, rhs)
            else:
                result = bool(lhs == rhs)
            if not result:
                rule_matched = False
            condition_traces.append(
                {
                    "feature": feature_name,
                    "op": op,
                    "threshold": condition["threshold"],
                    "actual": round(context.to_float(lhs), 9),
                    "threshold_in_precision": round(context.to_float(rhs), 9),
                    "margin": round(context.to_float(margin_internal), 9),
                    "result": bool(result),
                }
            )
        rule_trace = {
            "rule_id": rule["rule_id"],
            "rule_number": rule["rule_number"],
            "generation": rule.get("generation", 1),
            "conditions": condition_traces,
            "matched": rule_matched,
        }
        path.append(rule_trace)
        if rule_matched:
            matches.append(rule)
            label = rule["label"]
            confidence = context.put(rule["confidence"])
            weight = context.put(rule.get("weight", 1.0))
            contribution = context.mul(confidence, weight)
            supports[label] = context.add(supports.get(label, context.put(0.0)), contribution)

    if matches:
        selected = min(matches, key=lambda rule: (rule["rule_number"], rule["rule_id"]))
        selected_label = selected["label"]
        matching_same_label = [rule for rule in matches if rule["label"] == selected_label]
        weighted_sum = context.put(0.0)
        weight_sum = context.put(0.0)
        for rule in matching_same_label:
            confidence = context.put(rule["confidence"])
            weight = context.put(rule.get("weight", 1.0))
            weighted_sum = context.add(weighted_sum, context.mul(confidence, weight))
            weight_sum = context.add(weight_sum, weight)
        confidence_value = context.div(weighted_sum, weight_sum)
        selected_support = supports.get(selected_label, context.put(0.0))
        runner_up = context.put(0.0)
        for label, support in supports.items():
            if label != selected_label and context.to_float(support) > context.to_float(runner_up):
                runner_up = support
        class_margin = context.sub(selected_support, runner_up)
        result_label = selected_label
        selected_rule_id = selected["rule_id"]
    else:
        result_label = tree["default_label"]
        confidence_value = context.put(tree["default_confidence"])
        selected_rule_id = "DEFAULT"
        selected_support = confidence_value
        class_margin = confidence_value

    return {
        "label": result_label,
        "confidence": round(context.to_float(confidence_value), 9),
        "class_margin": round(context.to_float(class_margin), 9),
        "support_by_label": {
            label: round(context.to_float(value), 9) for label, value in supports.items()
        },
        "selected_rule_id": selected_rule_id,
        "matching_rule_ids": [rule["rule_id"] for rule in matches],
        "decision_path": path,
        "feature_values": {key: round(value, 9) for key, value in feature_floats.items()},
        "rule_version": tree["source_version"],
        "version_header": tree["version_header"],
        "tree_rev": tree["tree_rev"],
        "rule_generation": tree.get("rule_generation")
        or (selected.get("generation", 1) if matches else 0),
        "tree_hash": tree_hash(tree),
    }


def migrate_v1_to_v2(tree: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    if tree.get("source_version") != "v1":
        tree = parse_v1_tree(tree) if isinstance(tree, dict) else load_decision_tree(tree)
    completion: list[dict[str, Any]] = []
    rules: list[dict[str, Any]] = []
    meta: list[dict[str, Any]] = []
    for rule in tree["rules"]:
        completion.append(
            {"rule_id": rule["rule_id"], "field": "generation", "source": "default:generation=1"}
        )
        completion.append(
            {
                "rule_id": rule["rule_id"],
                "field": "time_ms",
                "source": "conversion:relative_seconds*1000",
            }
        )
        completion.append(
            {"rule_id": rule["rule_id"], "field": "tombstone", "source": "default:null"}
        )
        rules.append(
            {
                "rule_id": rule["rule_id"],
                "time_ms": rule["time_ms"],
                "all_of": rule["conditions"],
                "label": rule["label"],
                "confidence": rule["confidence"],
                "weight": rule.get("weight", 1.0),
            }
        )
        meta.append(
            {
                "rule_id": rule["rule_id"],
                "generation": 1,
                "active": 1,
                "tombstone": 0,
                "tombstone_reason": None,
                "first_seen_ms": rule["time_ms"],
                "updated_ms": rule["time_ms"],
            }
        )
    manifest = {
        "format": "prosody-decision-tree-shards",
        "version": "2.0",
        "tree_id": tree["tree_id"] + "-migrated-v2",
        "tree_rev": tree["tree_rev"],
        "rule_generation": 1,
        "default_label": tree["default_label"],
        "default_confidence": tree["default_confidence"],
        "shards": ["migrated-000.json"],
        "sqlite": "meta/migrated.db",
    }
    package = {
        "manifest": manifest,
        "shards": [
            {"shard_file": "migrated-000.json", "payload": {"shard": "migrated-000", "rules": rules}}
        ],
        "rule_meta": meta,
        "tree_meta": {"tree_rev": str(tree["tree_rev"]), "rule_generation": "1"},
    }
    report = {"direction": "v1_to_v2", "completion_sources": completion, "losses": []}
    return package, report


def write_migrated_v2(path: str | Path, package: dict[str, Any]) -> Path:
    root = Path(path)
    return write_v2_package(
        root,
        package["manifest"],
        package["shards"],
        package["rule_meta"],
        package["tree_meta"],
    )


def migrate_v2_to_v1(
    tree: dict[str, Any], *, tombstone_policy: str = "discard", strict_equivalence: bool = False
) -> tuple[dict[str, Any], dict[str, Any]]:
    if tree.get("source_version") != "v2":
        tree = load_decision_tree(tree) if not isinstance(tree, dict) else tree
    discarded: list[str] = []
    flattened: list[str] = []
    losses: list[dict[str, Any]] = []
    rule_lines: list[str] = []
    for rule in tree["rules"]:
        if rule.get("tombstone") or not rule.get("active", True):
            discarded.append(rule["rule_id"])
            losses.append({"rule_id": rule["rule_id"], "reason": "tombstone_cannot_be_represented_in_v1"})
            continue
        if rule.get("nested_source_rule_id"):
            flattened.append(rule["rule_id"])
        condition = rule["conditions"][0]
        if len(rule["conditions"]) != 1:
            # Canonical DNF paths are conjunctions. The pipe v1 line keeps the
            # first condition; callers needing every conjunction should use the
            # structured flat JSON view instead of the compact line format.
            losses.append(
                {
                    "rule_id": rule["rule_id"],
                    "reason": "multi_condition_pipe_line_uses_structured_flat_record",
                }
            )
        time_sec = rule["time_ms"] / 1000.0
        rule_lines.append(
            "|".join(
                [
                    rule["rule_id"],
                    f"{time_sec:g}",
                    condition["feature"],
                    condition["op"],
                    repr(condition["threshold"]),
                    rule["label"],
                    repr(rule["confidence"]),
                ]
            )
        )
    report = {
        "direction": "v2_to_v1",
        "discarded_tombstone_ids": discarded,
        "flattened_rule_ids": flattened,
        "losses": losses,
        "tombstone_policy": tombstone_policy,
    }
    if strict_equivalence and losses:
        raise DiagnosticError(
            E_SEMANTIC_LOSS,
            "v2 semantics cannot be represented with full fidelity in v1",
            field="rules",
            details=report,
        )
    v1 = {
        "format": "prosody-decision-tree",
        "version": "1.0",
        "tree_id": tree["tree_id"] + "-migrated-v1",
        "time_base": "relative_seconds",
        "tree_rev": tree["tree_rev"],
        "default_label": tree["default_label"],
        "default_confidence": tree["default_confidence"],
        "rule_lines": rule_lines,
    }
    return v1, report
