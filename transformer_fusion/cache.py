"""Content-addressed cache, node fingerprints and partial invalidation diagnostics."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

import numpy as np

from .errors import (
    Diagnostic,
    E_CACHE_CORRUPT,
    E_INCONSISTENT,
    E_RESOURCE_LIMIT,
)
from .manifest import load_tasks, task_from_mapping
from .model import CaseOutcome, ExecutionReport, ResourceLimits, TaskCase
from .util import (
    LockedPath,
    atomic_write_bytes,
    atomic_write_json,
    canonical_json,
    content_hash_array,
    digest_array,
    ensure_dir,
    float_hex,
    npy_bytes,
    npy_load_bytes,
    read_json,
    sha256_bytes,
    sha256_json,
    sha256_text,
)


CACHE_SCHEMA = "tf-cache-index-v1"
ENTRY_SCHEMA = "tf-cache-entry-v1"


class CacheCorruption(RuntimeError):
    def __init__(self, case_id: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.case_id = case_id
        self.details = dict(details or {})


def _index_body(index: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": index.get("schema", CACHE_SCHEMA),
        "version": int(index.get("version", 0)),
        "entries": index.get("entries", {}),
    }


def _entry_body(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in entry.items() if k != "checksum"}


@dataclass
class CacheLookup:
    key: str
    hit: bool
    array: np.ndarray | None
    entry: dict[str, Any] | None
    diagnostic: Diagnostic | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "hit": self.hit,
            "artifact_sha256": self.entry.get("artifact_sha256") if self.entry else None,
            "entry_checksum": self.entry.get("checksum") if self.entry else None,
            "entry_version": self.entry.get("version") if self.entry else None,
            "diagnostic": self.diagnostic.to_dict() if self.diagnostic else None,
        }


class CacheStore:
    """Atomic content-addressed array cache with versioned, checksummed entries."""

    def __init__(self, root: str | Path) -> None:
        self.root = ensure_dir(root)
        self.index_path = self.root / "index.json"
        self.artifacts = ensure_dir(self.root / "artifacts")

    def _empty_index(self) -> dict[str, Any]:
        body = {"schema": CACHE_SCHEMA, "version": 0, "entries": {}}
        body["checksum"] = sha256_json(body)
        return body

    def _load_index_unlocked(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return self._empty_index()
        try:
            value = read_json(self.index_path)
        except (OSError, json.JSONDecodeError) as exc:
            raise CacheCorruption("*", f"cache index is unreadable: {exc}") from exc
        if not isinstance(value, Mapping):
            raise CacheCorruption("*", "cache index must be an object")
        body = _index_body(value)
        if value.get("schema") != CACHE_SCHEMA:
            raise CacheCorruption("*", "cache index schema mismatch")
        if value.get("checksum") != sha256_json(body):
            raise CacheCorruption("*", "cache index checksum mismatch")
        return dict(value)

    def _save_index_unlocked(self, index: Mapping[str, Any]) -> None:
        body = _index_body(index)
        output = dict(body)
        output["checksum"] = sha256_json(body)
        atomic_write_json(self.index_path, output)

    def _validated_entry_unlocked(
        self,
        entry: Mapping[str, Any],
        *,
        require_artifact: bool,
    ) -> tuple[dict[str, Any], bytes | None]:
        if entry.get("schema") != ENTRY_SCHEMA:
            raise CacheCorruption(str(entry.get("case_id", "*")), "cache entry schema mismatch")
        if entry.get("checksum") != sha256_json(_entry_body(entry)):
            raise CacheCorruption(str(entry.get("case_id", "*")), "cache entry checksum mismatch")
        key = str(entry.get("key", ""))
        if len(key) != 64 or any(ch not in "0123456789abcdef" for ch in key):
            raise CacheCorruption(str(entry.get("case_id", "*")), "cache key is not a sha256 hex digest")
        artifact_name = str(entry.get("artifact", ""))
        expected = f"artifacts/{key}.npy"
        if artifact_name != expected:
            raise CacheCorruption(str(entry.get("case_id", "*")), "cache artifact path is not canonical")
        artifact_path = self.root / artifact_name
        if not artifact_path.exists():
            if require_artifact:
                raise CacheCorruption(str(entry.get("case_id", "*")), "cache artifact is missing")
            return dict(entry), None
        data = artifact_path.read_bytes()
        actual = sha256_bytes(data)
        if actual != entry.get("artifact_sha256"):
            raise CacheCorruption(
                str(entry.get("case_id", "*")),
                "cache artifact checksum mismatch",
                {"expected": entry.get("artifact_sha256"), "actual": actual},
            )
        return dict(entry), data

    def get(self, key: str) -> CacheLookup:
        try:
            with LockedPath(self.root):
                index = self._load_index_unlocked()
                entry = index.get("entries", {}).get(key)
                if entry is None:
                    return CacheLookup(key=key, hit=False, array=None, entry=None)
                validated, data = self._validated_entry_unlocked(entry, require_artifact=True)
                assert data is not None
                array = npy_load_bytes(data)
                return CacheLookup(key=key, hit=True, array=array, entry=validated)
        except (CacheCorruption, OSError, ValueError) as exc:
            case_id = getattr(exc, "case_id", "*")
            details = getattr(exc, "details", {})
            return CacheLookup(
                key=key,
                hit=False,
                array=None,
                entry=None,
                diagnostic=Diagnostic(
                    code=E_CACHE_CORRUPT,
                    case_id=case_id,
                    stage="cache",
                    message=str(exc),
                    details={"key": key, **dict(details)},
                ),
            )

    def put(
        self,
        key: str,
        array: np.ndarray,
        *,
        case_id: str,
        node_type: str,
        metadata: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Store a complete artifact atomically; return the committed entry."""

        payload = npy_bytes(array)
        artifact_sha = sha256_bytes(payload)
        with LockedPath(self.root):
            index = self._load_index_unlocked()
            existing = index.get("entries", {}).get(key)
            if existing is not None:
                try:
                    validated, data = self._validated_entry_unlocked(existing, require_artifact=True)
                    if data is not None and sha256_bytes(data) == artifact_sha:
                        return {
                            "status": "reused",
                            "key": key,
                            "version": validated.get("version"),
                            "checksum": validated.get("checksum"),
                            "artifact_sha256": validated.get("artifact_sha256"),
                            "entry": validated,
                        }
                except CacheCorruption:
                    pass
            new_version = int(index.get("version", 0)) + 1
            artifact_path = self.artifacts / f"{key}.npy"
            atomic_write_bytes(artifact_path, payload)
            entry_body = {
                "schema": ENTRY_SCHEMA,
                "key": key,
                "case_id": case_id,
                "node_type": node_type,
                "version": new_version,
                "artifact": f"artifacts/{key}.npy",
                "artifact_sha256": artifact_sha,
                "array": {
                    "shape": list(array.shape),
                    "dtype": str(array.dtype),
                    "nbytes": int(array.nbytes),
                    "digest": digest_array(array, domain=f"cache:{node_type}"),
                },
                "metadata": dict(metadata),
            }
            entry = dict(entry_body)
            entry["checksum"] = sha256_json(entry_body)
            entries = dict(index.get("entries", {}))
            entries[key] = entry
            index = {"schema": CACHE_SCHEMA, "version": new_version, "entries": entries}
            self._save_index_unlocked(index)
            return {
                "status": "computed",
                "key": key,
                "version": new_version,
                "checksum": entry["checksum"],
                "artifact_sha256": artifact_sha,
                "entry": entry,
            }

    def entry(self, key: str) -> dict[str, Any] | None:
        try:
            with LockedPath(self.root):
                index = self._load_index_unlocked()
                entry = index.get("entries", {}).get(key)
                if entry is None:
                    return None
                validated, _ = self._validated_entry_unlocked(entry, require_artifact=False)
                return validated
        except (CacheCorruption, OSError, ValueError):
            return None

    def summary(self) -> dict[str, Any]:
        try:
            with LockedPath(self.root):
                index = self._load_index_unlocked()
                entries = index.get("entries", {})
                return {
                    "root": str(self.root),
                    "version": int(index.get("version", 0)),
                    "checksum": index.get("checksum"),
                    "entry_count": len(entries),
                    "case_ids": sorted({str(e.get("case_id")) for e in entries.values()}),
                }
        except (CacheCorruption, OSError, ValueError) as exc:
            return {
                "root": str(self.root),
                "version": None,
                "checksum": None,
                "entry_count": 0,
                "diagnostic": Diagnostic(
                    code=E_CACHE_CORRUPT,
                    case_id=getattr(exc, "case_id", "*"),
                    stage="cache",
                    message=str(exc),
                ).to_dict(),
            }


def op_chain_digest(task: TaskCase) -> str:
    return sha256_json({"version": "tf-op-chain-v1", "op_chain": list(task.op_chain)})


def precision_tier(dtype: str) -> str:
    return {"float16": "fp16", "float32": "fp32", "float64": "fp64"}.get(dtype, dtype)


def fused_cache_key(
    task: TaskCase,
    input_sha256: str,
    *,
    effective_block_size: int | None = None,
    effective_dtype: str | None = None,
    tier: str | None = None,
) -> str:
    dtype = effective_dtype or task.dtype
    payload = {
        "schema": "tf-fused-cache-key-v1",
        "input_sha256": input_sha256,
        "op_chain_digest": op_chain_digest(task),
        "op_chain": list(task.op_chain),
        "axis_order": list(task.axis_order),
        "requested_block_size": int(task.block_size),
        "effective_block_size": int(effective_block_size or task.block_size),
        "eps_hex": float_hex(task.eps),
        "dtype": dtype,
        "precision_tier": tier or precision_tier(dtype),
        "activation_kind": task.activation_kind,
        "input_shape": list(task.input_shape),
    }
    return sha256_json(payload)


def activation_node_key(
    task: TaskCase,
    input_sha256: str,
    *,
    effective_dtype: str | None = None,
    tier: str | None = None,
) -> str:
    dtype = effective_dtype or task.dtype
    payload = {
        "schema": "tf-activation-node-v1",
        "input_sha256": input_sha256,
        "op_chain_digest": op_chain_digest(task),
        "activation_kind": task.activation_kind,
        "eps_hex": float_hex(task.eps),
        "block_size": int(task.block_size),
        "dtype": dtype,
        "precision_tier": tier or precision_tier(dtype),
        "shape": list(task.input_shape),
    }
    return sha256_json(payload)


def transpose_node_key(
    task: TaskCase,
    input_sha256: str,
    *,
    effective_dtype: str | None = None,
    tier: str | None = None,
) -> str:
    payload = {
        "schema": "tf-transpose-node-v1",
        "activation_key": activation_node_key(
            task, input_sha256, effective_dtype=effective_dtype, tier=tier
        ),
        "axis_order": list(task.axis_order),
        "shape": list(task.input_shape),
    }
    return sha256_json(payload)


def axis_mapping_key(task: TaskCase) -> str:
    """Fingerprint the pure axis mapping, independent of activation values."""

    return sha256_json(
        {
            "schema": "tf-axis-mapping-v1",
            "op_chain_digest": op_chain_digest(task),
            "axis_order": list(task.axis_order),
            "input_shape": list(task.input_shape),
            "transpose_indices": [
                index
                for index, op in enumerate(task.op_chain)
                if op in {"transpose", "permute"}
            ],
        }
    )


def downstream_node_key(
    task: TaskCase,
    input_sha256: str,
    op_index: int,
    *,
    effective_block_size: int | None = None,
    effective_dtype: str | None = None,
    tier: str | None = None,
) -> str:
    payload = {
        "schema": "tf-downstream-node-v1",
        "fused_key": fused_cache_key(
            task,
            input_sha256,
            effective_block_size=effective_block_size,
            effective_dtype=effective_dtype,
            tier=tier,
        ),
        "op_index": int(op_index),
        "op": task.op_chain[op_index],
    }
    return sha256_json(payload)


def case_node_ids(task: TaskCase) -> list[str]:
    nodes = ["input", "activation", "axis_mapping", "transpose", "fused"]
    # Every operation after the first transpose is represented as downstream.
    transpose_index = next(
        (i for i, op in enumerate(task.op_chain) if op in {"transpose", "permute"}),
        None,
    )
    if transpose_index is not None:
        for index in range(transpose_index + 1, len(task.op_chain)):
            nodes.append(f"downstream:{index}:{task.op_chain[index]}")
    nodes.append("final")
    return nodes


def node_keys(
    task: TaskCase,
    input_sha256: str,
    *,
    effective_block_size: int | None = None,
    effective_dtype: str | None = None,
    tier: str | None = None,
) -> dict[str, str]:
    fused = fused_cache_key(
        task,
        input_sha256,
        effective_block_size=effective_block_size,
        effective_dtype=effective_dtype,
        tier=tier,
    )
    activation = activation_node_key(
        task, input_sha256, effective_dtype=effective_dtype, tier=tier
    )
    transpose = transpose_node_key(
        task, input_sha256, effective_dtype=effective_dtype, tier=tier
    )
    output: dict[str, str] = {
        "input": sha256_json(
            {
                "schema": "tf-input-node-v1",
                "input_sha256": input_sha256,
                "shape": list(task.input_shape),
                "dtype": task.dtype,
            }
        ),
        "activation": activation,
        "axis_mapping": axis_mapping_key(task),
        "transpose": transpose,
        "fused": fused,
        "final": fused,
    }
    for node in case_node_ids(task):
        if node.startswith("downstream:"):
            index = int(node.split(":", 2)[1])
            output[node] = downstream_node_key(
                task,
                input_sha256,
                index,
                effective_block_size=effective_block_size,
                effective_dtype=effective_dtype,
                tier=tier,
            )
    return output


def resolve_input_node_hash(
    task: TaskCase,
    arrays: Mapping[str, np.ndarray] | None = None,
    *,
    data_dir: str | Path = "data",
) -> str:
    if arrays and task.case_id in arrays:
        return content_hash_array(np.asarray(arrays[task.case_id]))
    base = Path(data_dir)
    candidates: list[Path] = []
    if task.input_file:
        raw = Path(task.input_file)
        candidates.append(raw if raw.is_absolute() else base / raw)
    candidates.extend([base / "cases" / f"{task.case_id}.npz", base / "inputs.npz"])
    for path in candidates:
        if not path.exists():
            continue
        with np.load(path, allow_pickle=False) as payload:
            key = "input" if "input" in payload.files else task.case_id
            if key not in payload.files and task.case_id in payload.files:
                key = task.case_id
            if key in payload.files:
                return content_hash_array(payload[key])
    return ""


def _changed_fields(
    before: TaskCase,
    after: TaskCase,
    before_input_hash: str,
    after_input_hash: str,
) -> list[str]:
    fields: list[str] = []
    if before_input_hash != after_input_hash:
        fields.append("input_tensor")
    checks = (
        "op_chain",
        "input_shape",
        "axis_order",
        "activation_kind",
        "block_size",
        "eps",
        "dtype",
        "policy",
        "tolerance",
    )
    for field in checks:
        if getattr(before, field) != getattr(after, field):
            fields.append(field)
    return fields


def classify_case_invalidation(
    before: TaskCase,
    after: TaskCase,
    *,
    before_input_hash: str,
    after_input_hash: str,
) -> dict[str, Any]:
    changed = _changed_fields(before, after, before_input_hash, after_input_hash)
    nodes = case_node_ids(before)
    before_keys = node_keys(before, before_input_hash)
    after_keys = node_keys(after, after_input_hash)
    changed_set = set(changed)
    invalid_set: set[str] = set()
    activation_fields = {
        "input_tensor",
        "input_shape",
        "op_chain",
        "activation_kind",
        "block_size",
        "dtype",
        "eps",
    }
    mapping_fields = {"input_shape", "op_chain", "axis_order"}
    if "input_tensor" in changed_set:
        invalid_set.add("input")
    if changed_set & activation_fields:
        invalid_set.add("activation")
    if changed_set & mapping_fields:
        invalid_set.add("axis_mapping")
    if {"activation", "axis_mapping"} & invalid_set:
        invalid_set.add("transpose")
        invalid_set.add("fused")
    if "transpose" in invalid_set or "fused" in invalid_set:
        for node in nodes:
            if node.startswith("downstream:"):
                invalid_set.add(node)
        invalid_set.add("final")
    invalid = [node for node in nodes if node in invalid_set]
    # Policy/tolerance changes affect the plan/snapshot, but not cached tensor
    # content.  They intentionally leave all content nodes reusable.
    invalid = [node for node in nodes if node in set(invalid)]
    reused = [node for node in nodes if node not in set(invalid)]
    first_invalid_index = nodes.index(invalid[0]) if invalid else None
    tail = nodes[first_invalid_index:] if first_invalid_index is not None else []
    suffix = [node for node in tail if node in invalid_set]
    reused_in_suffix = [node for node in tail if node not in invalid_set]
    return {
        "changed_fields": changed,
        "all_nodes": nodes,
        "invalidated_nodes": invalid,
        "reused_nodes": reused,
        "recompute_suffix": suffix,
        "reused_in_suffix": reused_in_suffix,
        "before_keys": before_keys,
        "after_keys": after_keys,
        "invalidated_keys": {node: before_keys[node] for node in invalid},
        "reused_keys": {node: before_keys[node] for node in reused},
        "changed_keys": {
            node: {"before": before_keys[node], "after": after_keys[node]}
            for node in nodes
            if before_keys[node] != after_keys[node]
        },
        "no_intersection": not (set(invalid) & set(reused)),
        "covers_all": set(invalid) | set(reused) == set(nodes),
    }


def cache_diagnose(
    base_manifest: str | Path | Sequence[TaskCase | Mapping[str, Any]] | None = None,
    changed_manifest: str | Path | Sequence[TaskCase | Mapping[str, Any]] | None = None,
    *,
    changes: Mapping[str, Mapping[str, Any]] | None = None,
    data_dir: str | Path = "data",
    cache_dir: str | Path = "cache",
    snapshot_dir: str | Path = "snapshots",
    input_arrays: Mapping[str, np.ndarray] | None = None,
    limits: ResourceLimits | Mapping[str, Any] | None = None,
    execute_changed: bool = True,
    abs_tol: float | None = None,
    rel_tol: float | None = None,
) -> dict[str, Any]:
    """Compare two task generations and report exact cache invalidation sets.

    Unchanged cases are cache hits.  Changed cases are re-executed; the report
    records the dependency suffix that must be rebuilt and the invariant that
    invalidated and reused node sets are disjoint and cover every node.
    """

    from .engine import execute

    base_tasks = load_tasks(base_manifest)
    if changed_manifest is not None:
        loaded_changed = load_tasks(changed_manifest)
        changed_map = {task.case_id: task for task in loaded_changed}
        changed_tasks = [changed_map.get(task.case_id, task) for task in base_tasks]
    else:
        change_map = {str(k): dict(v) for k, v in (changes or {}).items()}
        changed_tasks = []
        for task in base_tasks:
            updates = change_map.get(task.case_id)
            if not updates:
                changed_tasks.append(task)
                continue
            changed_tasks.append(task.evolved(**updates))

    base_hashes = {
        task.case_id: resolve_input_node_hash(task, input_arrays, data_dir=data_dir)
        for task in base_tasks
    }
    changed_hashes = {
        task.case_id: resolve_input_node_hash(task, input_arrays, data_dir=data_dir)
        for task in changed_tasks
    }
    base_map = {task.case_id: task for task in base_tasks}
    changed_map2 = {task.case_id: task for task in changed_tasks}

    affected_cases: list[str] = []
    unchanged_cases: list[str] = []
    case_reports: dict[str, Any] = {}
    invalidated_nodes: dict[str, list[str]] = {}
    reused_nodes: dict[str, list[str]] = {}
    recompute_suffix: dict[str, list[str]] = {}
    reused_in_suffix: dict[str, list[str]] = {}
    invalidated_keys: dict[str, dict[str, str]] = {}
    reused_keys: dict[str, dict[str, str]] = {}
    diagnostics: list[Diagnostic] = []

    for case_id, before in base_map.items():
        after = changed_map2.get(case_id, before)
        changed = _changed_fields(before, after, base_hashes[case_id], changed_hashes[case_id])
        if changed:
            affected_cases.append(case_id)
        else:
            unchanged_cases.append(case_id)
        report = classify_case_invalidation(
            before,
            after,
            before_input_hash=base_hashes[case_id],
            after_input_hash=changed_hashes[case_id],
        )
        case_reports[case_id] = report
        invalidated_nodes[case_id] = report["invalidated_nodes"]
        reused_nodes[case_id] = report["reused_nodes"]
        recompute_suffix[case_id] = report["recompute_suffix"]
        reused_in_suffix[case_id] = report["reused_in_suffix"]
        invalidated_keys[case_id] = report["invalidated_keys"]
        reused_keys[case_id] = report["reused_keys"]
        if not report["no_intersection"] or not report["covers_all"]:
            diagnostics.append(
                Diagnostic(
                    code=E_INCONSISTENT,
                    case_id=case_id,
                    stage="cache_invalidation",
                    message="invalidated/reused node partition is inconsistent",
                    details=report,
                )
            )

    # Populate/reuse the baseline cache first.  This makes the changed run's
    # reuse decisions observable and keeps every write atomic.
    baseline_report = execute(
        base_tasks,
        data_dir=data_dir,
        input_arrays=input_arrays,
        cache_dir=cache_dir,
        snapshot_dir=snapshot_dir,
        limits=limits,
        abs_tol=abs_tol,
        rel_tol=rel_tol,
    )

    reuse_report: ExecutionReport | None = None
    if unchanged_cases:
        reuse_report = execute(
            [base_map[case_id] for case_id in unchanged_cases],
            data_dir=data_dir,
            input_arrays=input_arrays,
            cache_dir=cache_dir,
            snapshot_dir=snapshot_dir,
            limits=limits,
            abs_tol=abs_tol,
            rel_tol=rel_tol,
        )

    resource_limits = ResourceLimits.from_mapping(limits)
    parallel_cap = max(1, int(resource_limits.max_parallel_recompute_cases))
    waves = [
        affected_cases[index : index + parallel_cap]
        for index in range(0, len(affected_cases), parallel_cap)
    ]
    changed_reports: list[ExecutionReport] = []
    if execute_changed and affected_cases:
        for wave in waves:
            changed_reports.append(
                execute(
                    [changed_map2[case_id] for case_id in wave],
                    data_dir=data_dir,
                    input_arrays=input_arrays,
                    cache_dir=cache_dir,
                    snapshot_dir=snapshot_dir,
                    limits=limits,
                    abs_tol=abs_tol,
                    rel_tol=rel_tol,
                )
            )

    changed_outcomes: dict[str, CaseOutcome] = {}
    for report in changed_reports:
        for outcome in report.cases:
            changed_outcomes[outcome.case_id] = outcome
    verified_outcomes = (
        {outcome.case_id: outcome for outcome in reuse_report.cases}
        if reuse_report is not None
        else {outcome.case_id: outcome for outcome in baseline_report.cases}
    )

    cache_hit_cases: list[str] = []
    cache_miss_cases: list[str] = []
    unchanged_equivalence_ok = True
    changed_equivalence_ok = True
    for case_id in unchanged_cases:
        outcome = verified_outcomes.get(case_id)
        if outcome and outcome.cache.get("hit"):
            cache_hit_cases.append(case_id)
        if outcome and not outcome.equivalence.get("ok", False):
            unchanged_equivalence_ok = False
    for case_id, outcome in changed_outcomes.items():
        if outcome.cache.get("hit"):
            cache_hit_cases.append(case_id)
        else:
            cache_miss_cases.append(case_id)
        if not outcome.equivalence.get("ok", False):
            changed_equivalence_ok = False

    # Every invalidated node key must differ, and every reused node key must be
    # identical.  This is the executable form of the partial invalidation rule.
    for case_id, report in case_reports.items():
        for node, before_key in report["invalidated_keys"].items():
            after_key = report["after_keys"][node]
            if before_key == after_key:
                diagnostics.append(
                    Diagnostic(
                        code=E_INCONSISTENT,
                        case_id=case_id,
                        stage="cache_invalidation",
                        message=f"invalidated node reused its old key: {node}",
                        details={"node": node, "key": before_key},
                    )
                )
        for node, before_key in report["reused_keys"].items():
            after_key = report["after_keys"][node]
            if before_key != after_key:
                diagnostics.append(
                    Diagnostic(
                        code=E_INCONSISTENT,
                        case_id=case_id,
                        stage="cache_invalidation",
                        message=f"reused node changed key: {node}",
                        details={"node": node, "before": before_key, "after": after_key},
                    )
                )

    all_invalid = {
        f"{case_id}::{node}"
        for case_id, nodes in invalidated_nodes.items()
        for node in nodes
    }
    all_reused = {
        f"{case_id}::{node}"
        for case_id, nodes in reused_nodes.items()
        for node in nodes
    }
    no_global_intersection = not (all_invalid & all_reused)
    global_union_covers = all(
        set(invalidated_nodes[c]) | set(reused_nodes[c]) == set(case_reports[c]["all_nodes"])
        for c in case_reports
    )

    parallel_reduced = len(affected_cases) > parallel_cap
    if parallel_reduced:
        diagnostics.append(
            Diagnostic(
                code=E_RESOURCE_LIMIT,
                case_id="*",
                stage="cache_recompute",
                message="parallel recompute case cap exceeded; affected cases were serialized into waves",
                severity="warning",
                details={
                    "affected_cases": len(affected_cases),
                    "max_parallel_recompute_cases": parallel_cap,
                    "waves": len(waves),
                },
            )
        )

    result = {
        "ok": (
            baseline_report.ok
            and all(report.ok for report in changed_reports)
            and not any(d.severity == "error" for d in diagnostics)
            and unchanged_equivalence_ok
            and changed_equivalence_ok
        ),
        "affected_cases": affected_cases,
        "unchanged_cases": unchanged_cases,
        "changed_fields": {
            case_id: report["changed_fields"] for case_id, report in case_reports.items()
        },
        "invalidated_nodes": invalidated_nodes,
        "reused_nodes": reused_nodes,
        "recompute_suffix": recompute_suffix,
        "reused_in_suffix": reused_in_suffix,
        "invalidated_keys": invalidated_keys,
        "reused_keys": reused_keys,
        "key_changes": {
            case_id: report["changed_keys"] for case_id, report in case_reports.items()
        },
        "no_intersection": no_global_intersection,
        "union_covers_all_affected_nodes": global_union_covers,
        "recomputed_cases": sorted(changed_outcomes),
        "cache_hit_cases": sorted(set(cache_hit_cases)),
        "cache_miss_cases": sorted(set(cache_miss_cases)),
        "recompute_waves": waves,
        "max_parallel_recompute_cases": parallel_cap,
        "parallelism_reduced": parallel_reduced,
        "baseline_execution": baseline_report.to_dict(),
        "reuse_verification_execution": reuse_report.to_dict() if reuse_report is not None else None,
        "changed_execution": [report.to_dict() for report in changed_reports],
        "diagnostics": [d.to_dict() for d in diagnostics],
        "selfcheck": {
            "case_sets": {
                "base": sorted(base_map),
                "changed": sorted(changed_map2),
                "affected": sorted(affected_cases),
                "unchanged": sorted(unchanged_cases),
            },
            "invalid_reused_disjoint": no_global_intersection,
            "union_covers_all": global_union_covers,
            "unchanged_equivalence_ok": unchanged_equivalence_ok,
            "changed_equivalence_ok": changed_equivalence_ok,
        },
    }
    if not no_global_intersection or not global_union_covers:
        result["ok"] = False
        if not any(d.code == E_INCONSISTENT for d in diagnostics):
            diagnostics.append(
                Diagnostic(
                    code=E_INCONSISTENT,
                    case_id="*",
                    stage="cache_invalidation",
                    message="global node partition is inconsistent",
                    details={
                        "no_intersection": no_global_intersection,
                        "union_covers_all": global_union_covers,
                    },
                )
            )
            result["diagnostics"] = [d.to_dict() for d in diagnostics]
    return result
