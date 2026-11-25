from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .canonical import canonical_digest, file_digest, load_json
from .errors import (
    BaseLayerMissingError,
    ErrorCode,
    GenerationRollbackError,
    LayerConflictError,
    LayerForgeError,
    ShardHashMismatchError,
)
from .validation import require_int, require_nonempty_str, validate_key_values, validate_seq_continuity


DEFAULT_CONFIG: Dict[str, Any] = {
    "exact_candidate_threshold": 12,
    "exact_object_threshold": 24,
    "sample_bucket_count": 16,
    "sample_buckets": 8,
    "max_miss_bound": 0.60,
    "concurrency": 2,
    "round_budget_ms": 20,
    "clock_tolerance_ms": 2,
    "hash_algorithm": "sha256",
}


def load_config(sample_dir: Path) -> Dict[str, Any]:
    path = sample_dir / "build_config.json"
    config = dict(DEFAULT_CONFIG)
    if path.exists():
        raw = load_json(path)
        if not isinstance(raw, Mapping):
            raise LayerForgeError("build_config.json must contain an object")
        for key, value in raw.items():
            config[key] = value
    return config


def load_build_params(sample_dir: Path) -> Dict[str, Any]:
    path = sample_dir / "build_params.json"
    if not path.exists():
        return {"args": {}, "env": {}}
    raw = load_json(path)
    if not isinstance(raw, Mapping):
        raise LayerForgeError("build_params.json must contain an object")
    return {
        "args": validate_key_values(raw.get("args", {}), "build_params.args"),
        "env": validate_key_values(raw.get("env", {}), "build_params.env", env=True),
    }


def semantic_core(step: Mapping[str, Any], normalized_actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "step_id": step["step_id"],
        "seq": int(step["seq"]),
        "base_ref": step.get("base_ref") or step.get("base"),
        "depends_on": sorted(list(step.get("depends_on") or [])),
        "actions": [
            {
                "op": action["op"],
                "path": action["path"],
                "content": action["content"],
                "mode": action["mode"],
            }
            for action in normalized_actions
        ],
        "env": validate_key_values(step.get("env", {}), "step.env", env=True),
        "args": validate_key_values(step.get("args", {}), "step.args"),
        "work_cost_ms": step.get("work_cost_ms"),
    }


def convert_v1_time(step: Mapping[str, Any], clock_cfg: Mapping[str, Any]) -> Dict[str, Any]:
    seconds = require_int(
        step.get("written_after_seconds"),
        "step.written_after_seconds",
        minimum=0,
    )
    base_ms = require_int(
        clock_cfg.get("conversion_base_ms"),
        "logical_clock.conversion_base_ms",
    )
    scale = 1000
    absolute_ms = base_ms + seconds * scale
    return {
        "written_at_ms": absolute_ms,
        "conversion": {
            "source_format": "v1",
            "input_field": "written_after_seconds",
            "original_value": seconds,
            "original_unit": "second",
            "scale_ms_per_unit": scale,
            "offset_ms": base_ms,
            "rounding_mode": "exact_integer_multiplication",
            "rounding_direction": "none",
            "rounded": False,
            "absolute_ms": absolute_ms,
        },
    }


def _validate_base_refs(v1_steps: List[Dict[str, Any]], base_layers: Dict[str, Any]) -> None:
    ids = {step["step_id"] for step in v1_steps}
    seq_by_id = {step["step_id"]: int(step["seq"]) for step in v1_steps}
    for step in v1_steps:
        ref = step.get("base_ref") or step.get("base")
        require_nonempty_str(ref, f"step[{step.get('step_id')}].base_ref")
        assert isinstance(ref, str)
        resolved = ref
        if ref.startswith("step:"):
            resolved = ref.split(":", 1)[1]
        if ref not in base_layers:
            if resolved not in ids:
                raise BaseLayerMissingError(
                    "base layer reference cannot be resolved",
                    details={
                        "step_id": step["step_id"],
                        "base_ref": ref,
                        "known_base_layers": sorted(base_layers),
                        "known_steps": sorted(ids),
                    },
                )
            if seq_by_id[resolved] >= int(step["seq"]):
                raise BaseLayerMissingError(
                    "base reference must point to an earlier step or known base layer",
                    details={
                        "step_id": step["step_id"],
                        "base_ref": ref,
                        "referenced_seq": seq_by_id[resolved],
                        "step_seq": step["seq"],
                    },
                )

        for dep in step.get("depends_on") or []:
            dep_id = dep.split(":", 1)[1] if isinstance(dep, str) and dep.startswith("step:") else dep
            if dep_id not in ids:
                raise BaseLayerMissingError(
                    "dependency reference cannot be resolved",
                    details={"step_id": step["step_id"], "depends_on": dep},
                )


def _connect_sqlite(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise LayerForgeError(
            "v2 SQLite metadata database is missing",
            code=ErrorCode.VALIDATION,
            details={"path": str(path)},
        )
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    return conn


def _validate_sqlite_and_load_candidates(
    sample_dir: Path,
    sqlite_rel: str,
    shard_index_rows: List[Mapping[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    sqlite_path = sample_dir / sqlite_rel
    conn = _connect_sqlite(sqlite_path)
    try:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        required = {"meta", "layer_shards", "cache_entries", "tombstones"}
        missing = sorted(required - tables)
        if missing:
            raise LayerForgeError(
                "v2 SQLite metadata is missing required tables",
                details={"path": str(sqlite_path), "missing_tables": missing},
            )

        db_shards = [dict(row) for row in conn.execute("SELECT * FROM layer_shards").fetchall()]
        index_by_path = {item["path"]: item for item in shard_index_rows}
        for row in db_shards:
            path_text = require_nonempty_str(row.get("path"), "layer_shards.path")
            expected = require_nonempty_str(row.get("sha256"), "layer_shards.sha256")
            require_int(row.get("written_at_ms"), "layer_shards.written_at_ms")
            require_int(row.get("logical_clock_ms"), "layer_shards.logical_clock_ms", minimum=0)
            if path_text not in index_by_path:
                raise ShardHashMismatchError(
                    "SQLite shard is absent from v2 index",
                    details={"path": path_text, "sqlite": str(sqlite_path)},
                )
            actual = file_digest(sample_dir / path_text)
            if actual != expected or index_by_path[path_text].get("sha256") != expected:
                raise ShardHashMismatchError(
                    "v2 shard hash does not match SQLite or index",
                    details={
                        "path": path_text,
                        "expected_sha256": expected,
                        "actual_sha256": actual,
                        "index_sha256": index_by_path[path_text].get("sha256"),
                    },
                )

        candidates: List[Dict[str, Any]] = []
        rows = conn.execute(
            "SELECT * FROM cache_entries ORDER BY step_id ASC, logical_clock_ms ASC, generation ASC, rowid ASC"
        ).fetchall()
        generations: Dict[str, List[Tuple[int, int, str]]] = {}
        for row in rows:
            item = dict(row)
            if item.get("typeof_written") is not None:
                pass
            gen = require_int(item.get("generation"), "cache_entries.generation", minimum=1)
            clock_ms = require_int(
                item.get("logical_clock_ms"),
                "cache_entries.logical_clock_ms",
                minimum=0,
            )
            require_int(item.get("written_at_ms"), "cache_entries.written_at_ms")
            step_id = require_nonempty_str(item.get("step_id"), "cache_entries.step_id")
            key = require_nonempty_str(item.get("cache_key"), "cache_entries.cache_key")
            if not isinstance(item.get("tombstoned"), int) or item["tombstoned"] not in (0, 1):
                raise LayerForgeError(
                    "cache_entries.tombstoned must be 0 or 1",
                    details={"cache_key": key, "value": item.get("tombstoned")},
                )
            generations.setdefault(step_id, []).append((clock_ms, gen, key))
            candidates.append(
                {
                    "cache_key": key,
                    "step_id": step_id,
                    "content_digest": item.get("content_digest"),
                    "parent_digest": item.get("parent_digest"),
                    "source_version": "v2",
                    "cache_generation": gen,
                    "logical_clock_ms": clock_ms,
                    "tombstoned": bool(item["tombstoned"]),
                    "shard_id": item.get("shard_id"),
                    "candidate_kind": item.get("candidate_kind") or "historical",
                }
            )

        for step_id, sequence in generations.items():
            last_gen = None
            last_clock = None
            for clock_ms, gen, key in sequence:
                if last_gen is not None:
                    if clock_ms < last_clock:
                        raise GenerationRollbackError(
                            "cache logical clock moved backwards in SQLite history",
                            details={
                                "step_id": step_id,
                                "cache_key": key,
                                "clock_ms": clock_ms,
                                "previous_clock_ms": last_clock,
                            },
                        )
                    if gen < last_gen:
                        raise GenerationRollbackError(
                            "cache generation regressed in SQLite metadata",
                            details={
                                "step_id": step_id,
                                "cache_key": key,
                                "generation": gen,
                                "previous_generation": last_gen,
                                "logical_clock_ms": clock_ms,
                            },
                        )
                last_gen = gen
                last_clock = clock_ms

        tombstones = [dict(row) for row in conn.execute("SELECT * FROM tombstones").fetchall()]
        for tomb in tombstones:
            require_int(tomb.get("generation"), "tombstones.generation", minimum=1)
            require_nonempty_str(tomb.get("cache_key"), "tombstones.cache_key")
        meta = {row[0]: row[1] for row in conn.execute("SELECT key, value FROM meta").fetchall()}
        return candidates, tombstones, meta
    finally:
        conn.close()


def load_bundle(sample_dir: Path | str, *, workspace_root: Optional[Path] = None) -> Dict[str, Any]:
    sample_dir = Path(sample_dir)
    workspace_root = Path(workspace_root) if workspace_root else sample_dir / "workspace-root"
    workspace_root.mkdir(parents=True, exist_ok=True)

    v1_path = sample_dir / "v1_manifest.json"
    index_path = sample_dir / "v2_manifest.json"
    if not v1_path.exists():
        raise LayerForgeError("v1_manifest.json is missing", details={"sample_dir": str(sample_dir)})
    if not index_path.exists():
        raise LayerForgeError("v2_manifest.json is missing", details={"sample_dir": str(sample_dir)})
    v1 = load_json(v1_path)
    v2_index = load_json(index_path)
    config = load_config(sample_dir)
    params = load_build_params(sample_dir)

    if not isinstance(v1, Mapping) or v1.get("schema") != "layerforge.v1":
        raise LayerForgeError("v1 manifest has unsupported schema", details={"path": str(v1_path)})
    if not isinstance(v2_index, Mapping) or v2_index.get("schema") != "layerforge.v2-index/v1":
        raise LayerForgeError("v2 index has unsupported schema", details={"path": str(index_path)})

    base_layers = v1.get("base_layers")
    if not isinstance(base_layers, Mapping) or not base_layers:
        raise LayerForgeError("v1 base_layers must be a non-empty object")
    for ref, layer in base_layers.items():
        if not isinstance(layer, Mapping):
            raise LayerForgeError("base layer entry must be an object", details={"ref": ref})
        require_nonempty_str(layer.get("digest"), f"base_layers.{ref}.digest")

    v1_steps_raw = v1.get("steps")
    if not isinstance(v1_steps_raw, list) or not v1_steps_raw:
        raise LayerForgeError("v1 steps must be a non-empty list")
    validate_seq_continuity(v1_steps_raw)
    _validate_base_refs(v1_steps_raw, base_layers)

    clock_cfg = v1.get("logical_clock") or {}
    if not isinstance(clock_cfg, Mapping):
        raise LayerForgeError("v1 logical_clock must be an object")

    v1_steps: Dict[str, Dict[str, Any]] = {}
    for raw_step in v1_steps_raw:
        step_id = require_nonempty_str(raw_step.get("step_id"), "step.step_id")
        if step_id in v1_steps:
            raise LayerForgeError("duplicate step_id", details={"step_id": step_id})
        actions = _normalize_actions_late(raw_step.get("actions"), workspace_root)
        env = validate_key_values(raw_step.get("env", {}), "step.env", env=True)
        args = validate_key_values(raw_step.get("args", {}), "step.args")
        time_info = convert_v1_time(raw_step, clock_cfg)
        step = dict(raw_step)
        step["actions"] = actions
        step["env"] = env
        step["args"] = args
        step["time"] = time_info
        step["semantic_core"] = semantic_core(raw_step, actions)
        step["semantic_digest"] = canonical_digest(step["semantic_core"])
        v1_steps[step_id] = step

    shard_refs = v2_index.get("shards")
    if not isinstance(shard_refs, list) or not shard_refs:
        raise LayerForgeError("v2 index must declare at least one shard")
    for ref in shard_refs:
        if not isinstance(ref, Mapping) or not isinstance(ref.get("path"), str):
            raise LayerForgeError("invalid shard reference", details={"ref": ref})
        actual = file_digest(sample_dir / ref["path"])
        if actual != ref.get("sha256"):
            raise ShardHashMismatchError(
                "indexed v2 shard hash mismatch",
                details={
                    "path": ref["path"],
                    "expected_sha256": ref.get("sha256"),
                    "actual_sha256": actual,
                },
            )

    v2_steps: Dict[str, Dict[str, Any]] = {}
    for ref in shard_refs:
        shard = load_json(sample_dir / ref["path"])
        if not isinstance(shard, Mapping) or shard.get("schema") != "layerforge.v2-shard/v1":
            raise LayerForgeError("v2 shard has unsupported schema", details={"path": ref["path"]})
        for raw_step in shard.get("steps") or []:
            step_id = require_nonempty_str(raw_step.get("step_id"), "v2.step_id")
            if step_id in v2_steps:
                raise LayerForgeError("duplicate v2 step in shards", details={"step_id": step_id})
            require_int(raw_step.get("written_at_ms"), f"v2.{step_id}.written_at_ms")
            require_int(raw_step.get("cache_generation"), f"v2.{step_id}.cache_generation", minimum=1)
            require_nonempty_str(raw_step.get("cache_key"), f"v2.{step_id}.cache_key")
            v2_steps[step_id] = dict(raw_step)

    sqlite_rel = v2_index.get("sqlite") or "v2_metadata.sqlite3"
    candidates, tombstones, sqlite_meta = _validate_sqlite_and_load_candidates(
        sample_dir, sqlite_rel, shard_refs
    )

    for step_id, v1_step in v1_steps.items():
        v2_step = v2_steps.get(step_id)
        if v2_step is None:
            raise LayerConflictError(
                "v2 is missing a layer present in v1",
                details={"step_id": step_id},
            )
        v2_core = v2_step.get("semantic_core")
        if not isinstance(v2_core, Mapping):
            raise LayerConflictError(
                "v2 layer is missing semantic_core",
                details={"step_id": step_id},
            )
        v1_core = v1_step["semantic_core"]
        if canonical_digest(v2_core) != v2_step.get("semantic_digest"):
            raise LayerConflictError(
                "v2 semantic digest does not match canonical core",
                details={"step_id": step_id, "declared": v2_step.get("semantic_digest")},
            )
        if canonical_digest(v1_core) != canonical_digest(v2_core):
            differing = _dict_difference(v1_core, v2_core)
            raise LayerConflictError(
                "v1 and v2 layer fields are inconsistent",
                details={"step_id": step_id, "differing_fields": differing},
            )
        if int(v2_step["written_at_ms"]) != int(v1_step["time"]["written_at_ms"]):
            raise LayerConflictError(
                "v1 relative time and v2 absolute write time disagree",
                details={
                    "step_id": step_id,
                    "v1_absolute_ms": v1_step["time"]["written_at_ms"],
                    "v2_written_at_ms": v2_step["written_at_ms"],
                },
            )

    for step_id in v2_steps:
        if step_id not in v1_steps:
            raise LayerConflictError(
                "v2 contains a layer absent from v1",
                details={"step_id": step_id},
            )

    unified_steps = [
        _merge_step(step_id, v1_steps[step_id], v2_steps[step_id], candidates, v2_index)
        for step_id in sorted(v1_steps, key=lambda sid: int(v1_steps[sid]["seq"]))
    ]
    tombstone_keys = {item["cache_key"] for item in tombstones}
    for candidate in candidates:
        if candidate["cache_key"] in tombstone_keys:
            candidate["tombstoned"] = True

    return {
        "schema": "layerforge.unified/v1",
        "sample_dir": str(sample_dir.resolve()),
        "workspace_root": str(workspace_root.resolve()),
        "logical_clock": {
            **clock_cfg,
            "tolerance_ms": config.get("clock_tolerance_ms", clock_cfg.get("tolerance_ms", 2)),
        },
        "base_layers": base_layers,
        "steps": unified_steps,
        "cache_candidates": candidates,
        "tombstones": tombstones,
        "sqlite_meta": sqlite_meta,
        "config": config,
        "build_params": params,
        "source_priority": {"v1": int(v1.get("format_priority", 10)), "v2": int(v2_index.get("format_priority", 20))},
    }


def _normalize_actions_late(raw_actions: Any, workspace_root: Path) -> List[Dict[str, Any]]:
    from .layers import normalize_actions

    if not isinstance(raw_actions, list):
        raise LayerForgeError("step.actions must be a list")
    return normalize_actions(raw_actions, workspace_root=workspace_root)


def _dict_difference(left: Mapping[str, Any], right: Mapping[str, Any]) -> Dict[str, Any]:
    fields = sorted(set(left) | set(right))
    result = {}
    for field in fields:
        if left.get(field) != right.get(field):
            result[field] = {"v1": left.get(field), "v2": right.get(field)}
    return result


def _merge_step(
    step_id: str,
    v1_step: Mapping[str, Any],
    v2_step: Mapping[str, Any],
    all_candidates: List[Mapping[str, Any]],
    v2_index: Mapping[str, Any],
) -> Dict[str, Any]:
    v1_cache_entries = _v1_cache_candidates(v1_step)
    v2_current = {
        "source_version": "v2",
        "logical_clock_ms": int(v2_step.get("cache_logical_clock_ms") or v2_step["written_at_ms"]),
        "cache_generation": int(v2_step["cache_generation"]),
        "cache_key": v2_step["cache_key"],
    }
    v1_current = {
        "source_version": "v1",
        "logical_clock_ms": int(
            (v1_step.get("cache") or {}).get("logical_clock_ms")
            or v1_step["time"]["written_at_ms"]
        ),
        "cache_generation": int((v1_step.get("cache") or {}).get("cache_generation") or 1),
        "cache_key": (v1_step.get("cache") or {}).get("cache_key"),
    }
    priority = {"v1": 10, "v2": int(v2_index.get("format_priority", 20))}
    candidates = [v1_current, v2_current]
    winner = max(
        candidates,
        key=lambda item: (
            int(item["logical_clock_ms"]),
            priority[item["source_version"]],
            int(item["cache_generation"]),
        ),
    )
    if v1_current["logical_clock_ms"] == v2_current["logical_clock_ms"]:
        if priority["v2"] > priority["v1"]:
            winner = v2_current
            basis = "equal_logical_clock_format_priority_v2"
        else:
            basis = "equal_logical_clock_format_priority"
    elif winner["logical_clock_ms"] == max(v1_current["logical_clock_ms"], v2_current["logical_clock_ms"]):
        basis = "higher_declared_logical_clock"
    else:
        basis = "cache_generation_tie_break"

    step_candidates = [
        dict(candidate)
        for candidate in all_candidates
        if candidate.get("step_id") == step_id
    ]
    step_candidates.extend(v1_cache_entries)
    return {
        "step_id": step_id,
        "seq": int(v1_step["seq"]),
        "base_ref": v1_step.get("base_ref") or v1_step.get("base"),
        "depends_on": list(v1_step.get("depends_on") or []),
        "actions": v1_step["actions"],
        "env": v1_step["env"],
        "args": v1_step["args"],
        "arg_refs": list(v1_step.get("arg_refs") or []),
        "work_cost_ms": v1_step.get("work_cost_ms"),
        "time": v1_step["time"],
        "written_at_ms": v1_step["time"]["written_at_ms"],
        "time_conversion": v1_step["time"]["conversion"],
        "semantic_digest": v1_step["semantic_digest"],
        "sources": {
            "v1": {
                "present": True,
                "source_file": "v1_manifest.json",
                "original_time": {
                    "field": "written_after_seconds",
                    "value": v1_step["time"]["conversion"]["original_value"],
                    "unit": "second",
                },
                "cache": v1_step.get("cache") or {},
            },
            "v2": {
                "present": True,
                "source_file": v2_step.get("shard_path") or "v2_shards",
                "original_time": {
                    "field": "written_at_ms",
                    "value": v2_step["written_at_ms"],
                    "unit": "millisecond",
                },
                "cache_generation": v2_step["cache_generation"],
                "cache_key": v2_step["cache_key"],
            },
        },
        "adjudication": {
            "selected_source": winner["source_version"],
            "basis": basis,
            "logical_clock_ms": winner["logical_clock_ms"],
            "cache_generation": winner["cache_generation"],
            "cache_key": winner.get("cache_key"),
            "format_priority": priority,
            "candidate_versions": candidates,
        },
        "cache_candidates": step_candidates,
    }


def _v1_cache_candidates(v1_step: Mapping[str, Any]) -> List[Dict[str, Any]]:
    entries = v1_step.get("cache_entries")
    if entries is None:
        cache = v1_step.get("cache")
        entries = [cache] if isinstance(cache, Mapping) else []
    result = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        result.append(
            {
                "cache_key": require_nonempty_str(entry.get("cache_key"), "v1.cache.cache_key"),
                "step_id": v1_step["step_id"],
                "content_digest": entry.get("content_digest"),
                "parent_digest": entry.get("parent_digest"),
                "source_version": "v1",
                "cache_generation": int(entry.get("cache_generation") or 1),
                "logical_clock_ms": int(
                    entry.get("logical_clock_ms") or v1_step["time"]["written_at_ms"]
                ),
                "tombstoned": bool(entry.get("tombstoned", False)),
                "shard_id": None,
                "candidate_kind": entry.get("candidate_kind") or ("current" if index == 0 else "historical"),
            }
        )
    return result
