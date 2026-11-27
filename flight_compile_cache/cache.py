"""Versioned, shape-specialized LRU compilation cache."""

from __future__ import annotations

import copy
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .compiler import (
    FullCompileBaseline,
    compile_entry_units,
    compare_specialized_to_baseline,
    execute_specialized,
    full_compile_work,
    plan_fingerprint,
)
from .errors import (
    BusinessError,
    BusinessRuleError,
    E_EVENT_INVALID,
    E_MODEL_NOT_FOUND,
    E_SNAPSHOT_INTEGRITY,
    E_SNAPSHOT_PROJECT_MISMATCH,
    E_SNAPSHOT_SCHEMA,
    E_SNAPSHOT_VERSION_CHANGED,
    E_STALE_VERSION,
    E_VERSION_NOT_FOUND,
)
from .graph import (
    get_model,
    get_version_spec,
    model_graph_fingerprints,
    plan_request_graph,
    project_fingerprint,
    shape_segment,
    version_changed_units,
)
from .keys import (
    compile_key_readable,
    make_compile_key,
    normalized_input_shapes,
)
from .tensors import Tensor, make_input_tensor, tensor_fingerprint
from .util import atomic_write_json, canonical_json, deep_copy, read_json, sha256_hex

SNAPSHOT_SCHEMA = "flight-compile-cache-snapshot/v1"


@dataclass
class CacheEntry:
    compile_key: str
    model_id: str
    model_version: str
    input_shapes: dict[str, list[int]]
    shape_segment: str
    artifacts: dict[str, dict[str, Any]]
    plan_fingerprint: str
    compile_origin: str
    created_seq: int
    last_used_seq: int
    hit_count: int = 0
    compile_work: int = 0
    compile_ms: float = 0.0
    compiled_units: list[str] = field(default_factory=list)
    reused_units: list[str] = field(default_factory=list)
    unit_reports: list[dict[str, Any]] = field(default_factory=list)
    shape_specialized_kernel_compiles: int = 0
    created_at_time: float = 0
    registration_source: str = ""

    def artifact_ids(self) -> dict[str, str]:
        return {
            unit_id: artifact["artifact_id"]
            for unit_id, artifact in sorted(self.artifacts.items())
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "compile_key": self.compile_key,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "input_shapes": deep_copy(self.input_shapes),
            "shape_segment": self.shape_segment,
            "artifacts": deep_copy(self.artifacts),
            "plan_fingerprint": self.plan_fingerprint,
            "compile_origin": self.compile_origin,
            "created_seq": self.created_seq,
            "last_used_seq": self.last_used_seq,
            "hit_count": self.hit_count,
            "compile_work": self.compile_work,
            "compile_ms": self.compile_ms,
            "compiled_units": list(self.compiled_units),
            "reused_units": list(self.reused_units),
            "unit_reports": deep_copy(self.unit_reports),
            "shape_specialized_kernel_compiles": self.shape_specialized_kernel_compiles,
            "created_at_time": self.created_at_time,
            "registration_source": self.registration_source,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "CacheEntry":
        return cls(
            compile_key=str(value["compile_key"]),
            model_id=str(value["model_id"]),
            model_version=str(value["model_version"]),
            input_shapes={
                name: [int(dim) for dim in shape]
                for name, shape in value["input_shapes"].items()
            },
            shape_segment=str(value["shape_segment"]),
            artifacts=deep_copy(value["artifacts"]),
            plan_fingerprint=str(value["plan_fingerprint"]),
            compile_origin=str(value.get("compile_origin", "")),
            created_seq=int(value["created_seq"]),
            last_used_seq=int(value["last_used_seq"]),
            hit_count=int(value.get("hit_count", 0)),
            compile_work=int(value.get("compile_work", 0)),
            compile_ms=float(value.get("compile_ms", 0.0)),
            compiled_units=list(value.get("compiled_units", [])),
            reused_units=list(value.get("reused_units", [])),
            unit_reports=deep_copy(value.get("unit_reports", [])),
            shape_specialized_kernel_compiles=int(
                value.get("shape_specialized_kernel_compiles", 0)
            ),
            created_at_time=float(value.get("created_at_time", 0.0)),
            registration_source=str(value.get("registration_source", "")),
        )


class CompileCacheEngine:
    def __init__(
        self,
        project: Mapping[str, Any],
        *,
        cache_config_override: Mapping[str, Any] | None = None,
    ) -> None:
        self.project = project
        config = dict(project.get("cache_config", {}))
        if cache_config_override:
            config.update(cache_config_override)
        self.config = config
        self.capacity = int(config.get("capacity", 4))
        self.ms_per_work_unit = float(config.get("ms_per_work_unit", 0.0125))
        self.tolerance = float(config.get("numeric_tolerance", 1e-6))
        if self.capacity < 1:
            raise BusinessRuleError(E_SNAPSHOT_SCHEMA, "cache capacity must be >= 1")
        self.entries: OrderedDict[str, CacheEntry] = OrderedDict()
        self.artifact_registry: dict[str, dict[str, Any]] = {}
        self.key_registry: dict[str, dict[str, Any]] = {}
        self.current_versions: dict[str, str] = {
            model_id: str(model["initial_version"])
            for model_id, model in project.get("models", {}).items()
        }
        self.seq = 0
        self.request_records: list[dict[str, Any]] = []
        self.event_log: list[dict[str, Any]] = []
        self.eviction_log: list[dict[str, Any]] = []
        self.last_update_scope: dict[str, dict[str, Any]] = {}
        self.restore_report: dict[str, Any] | None = None
        self.baseline = FullCompileBaseline(project)
        self.stats: dict[str, Any] = {
            "valid_requests": 0,
            "rejected_requests": 0,
            "cache_hits": 0,
            "cache_misses": 0,
            "request_novel_key_compiles": 0,
            "request_recompiles_after_eviction": 0,
            "shape_specialization_events": 0,
            "shape_specialized_kernel_compiles": 0,
            "event_incremental_compiles": 0,
            "compiled_entries": 0,
            "compiled_units": 0,
            "reused_units": 0,
            "evictions": 0,
            "total_compile_work": 0,
            "total_compile_ms": 0.0,
            "baseline_full_compile_count": 0,
            "baseline_compile_work": 0,
            "baseline_compile_ms": 0.0,
            "consistent_requests": 0,
            "inconsistent_requests": 0,
            "expectation_mismatches": 0,
        }

    # ------------------------------------------------------------------
    # Request path
    # ------------------------------------------------------------------
    def process_request(
        self,
        request: Mapping[str, Any],
        *,
        origin: str = "request",
    ) -> dict[str, Any]:
        self.seq += 1
        if not isinstance(request, Mapping):
            self.stats["rejected_requests"] += 1
            self.stats["expectation_mismatches"] += 1
            record = {
                "kind": "request",
                "request_id": f"request-{self.seq}",
                "time": 0,
                "origin": origin,
                "status": "rejected",
                "cache_lookup": "skipped",
                "error_code": "E_REQUEST_SCHEMA",
                "errors": [BusinessError("E_REQUEST_SCHEMA", "request must be an object").to_dict()],
                "expected_match": False,
                "skipped": True,
            }
            self.request_records.append(record)
            return record
        request_id = str(request.get("id", f"request-{self.seq}"))
        request_time = float(request.get("time", 0.0))
        expected = dict(request.get("expectation", {"valid": True, "error_code": None}))
        record: dict[str, Any] = {
            "kind": "request",
            "request_id": request_id,
            "time": request_time,
            "origin": origin,
            "model_id": request.get("model_id"),
            "model_version": request.get("model_version"),
            "input_shapes": deep_copy(request.get("input_shapes", {})),
            "expected": expected,
        }
        try:
            plan = plan_request_graph(
                self.project,
                request,
                current_versions=self.current_versions,
            )
        except BusinessRuleError as exc:
            self.stats["rejected_requests"] += 1
            expected_code = expected.get("error_code")
            match = (not expected.get("valid", True)) and expected_code == exc.error.code
            if not match:
                self.stats["expectation_mismatches"] += 1
            record.update(
                {
                    "status": "rejected",
                    "cache_lookup": "skipped",
                    "error_code": exc.error.code,
                    "errors": [exc.error.to_dict()],
                    "expected_match": match,
                    "skipped": True,
                }
            )
            self.request_records.append(record)
            return record

        model_id = str(plan["model"]["model_id"])
        version = str(plan["version"])
        key = make_compile_key(model_id, version, plan["input_shapes"])
        segment = shape_segment(plan["graph"], plan["input_shapes"])
        input_tensors = self._make_inputs(request, plan)
        self.stats["valid_requests"] += 1

        entry = self.entries.get(key)
        evictions: list[dict[str, Any]] = []
        if entry is not None:
            hit = True
            self.stats["cache_hits"] += 1
            entry.hit_count += 1
            entry.last_used_seq = self.seq
            self.entries.move_to_end(key)
            key_meta = self.key_registry[key]
            key_meta["status"] = "resident"
            key_meta["times_hit"] = int(key_meta.get("times_hit", 0)) + 1
            compile_info = {
                "compiled_units": [],
                "reused_units": list(plan["topo_nodes"]),
                "unit_reports": [
                    {
                        "unit_id": unit_id,
                        "artifact_id": entry.artifacts[unit_id]["artifact_id"],
                        "scope": entry.artifacts[unit_id]["scope"],
                        "action": "cache_hit",
                        "compile_work": 0,
                        "compile_ms": 0.0,
                    }
                    for unit_id in plan["topo_nodes"]
                ],
                "compile_work": 0,
                "compile_ms": 0.0,
                "shape_specialized_kernel_compiles": 0,
                "source": "cache_hit",
            }
        else:
            hit = False
            self.stats["cache_misses"] += 1
            self.stats["shape_specialization_events"] += 1
            prior = self.key_registry.get(key)
            if prior is None or int(prior.get("times_compiled", 0)) == 0:
                self.stats["request_novel_key_compiles"] += 1
            elif prior.get("status") == "evicted":
                self.stats["request_recompiles_after_eviction"] += 1
            compile_origin = f"request:{request_id}:time={request_time:g}"
            compile_result = compile_entry_units(
                plan,
                key,
                artifact_registry=self.artifact_registry,
                ms_per_work_unit=self.ms_per_work_unit,
                compile_origin=compile_origin,
            )
            entry = CacheEntry(
                compile_key=key,
                model_id=model_id,
                model_version=version,
                input_shapes={
                    name: list(shape) for name, shape in plan["input_shapes"].items()
                },
                shape_segment=segment,
                artifacts=compile_result["artifacts"],
                plan_fingerprint=plan_fingerprint(plan),
                compile_origin=compile_origin,
                created_seq=self.seq,
                last_used_seq=self.seq,
                compiled_units=compile_result["compiled_units"],
                reused_units=compile_result["reused_units"],
                unit_reports=compile_result["unit_reports"],
                shape_specialized_kernel_compiles=compile_result[
                    "shape_specialized_kernel_compiles"
                ],
                compile_work=compile_result["compile_work"],
                compile_ms=compile_result["compile_ms"],
                created_at_time=request_time,
                registration_source=compile_origin,
            )
            self.entries[key] = entry
            self._register_key(
                key,
                plan,
                source=compile_origin,
                artifact_ids=entry.artifact_ids(),
                compile_ms=entry.compile_ms,
                compile_work=entry.compile_work,
                at_time=request_time,
                request_id=request_id,
            )
            self.stats["compiled_entries"] += 1
            self.stats["compiled_units"] += len(compile_result["compiled_units"])
            self.stats["reused_units"] += len(compile_result["reused_units"])
            self.stats["shape_specialized_kernel_compiles"] += compile_result[
                "shape_specialized_kernel_compiles"
            ]
            self.stats["total_compile_work"] += int(compile_result["compile_work"])
            self.stats["total_compile_ms"] += float(compile_result["compile_ms"])
            evictions = self._enforce_capacity(
                at_time=request_time,
                request_id=request_id,
            )
            compile_info = {
                "compiled_units": list(compile_result["compiled_units"]),
                "reused_units": list(compile_result["reused_units"]),
                "unit_reports": deep_copy(compile_result["unit_reports"]),
                "compile_work": compile_result["compile_work"],
                "compile_ms": compile_result["compile_ms"],
                "shape_specialized_kernel_compiles": compile_result[
                    "shape_specialized_kernel_compiles"
                ],
                "source": compile_origin,
            }
            update_scope = self.last_update_scope.get(model_id)
            if update_scope is not None:
                compile_info["update_incremental_scope"] = {
                    "event_id": update_scope["event_id"],
                    "from_version": update_scope["from_version"],
                    "to_version": update_scope["to_version"],
                    "affected_units": list(update_scope["affected_units"]),
                }

        key_meta = self.key_registry[key]
        key_meta["request_occurrences"] = int(key_meta.get("request_occurrences", 0)) + 1
        key_meta["last_request_id"] = request_id
        key_meta["status"] = "resident"

        baseline_result = self.baseline.compile_and_execute(plan, input_tensors)
        specialized_outputs = execute_specialized(plan, entry.artifacts, input_tensors)
        comparison = compare_specialized_to_baseline(
            specialized_outputs, baseline_result.outputs, self.tolerance
        )
        self.stats["baseline_full_compile_count"] += 1
        self.stats["baseline_compile_work"] += baseline_result.compile_work
        self.stats["baseline_compile_ms"] += baseline_result.compile_ms
        if comparison["consistent"]:
            self.stats["consistent_requests"] += 1
        else:
            self.stats["inconsistent_requests"] += 1

        expected_match = self._expected_match(expected, accepted=True, error_code=None)
        if not expected_match:
            self.stats["expectation_mismatches"] += 1
        record.update(
            {
                "status": "accepted",
                "cache_lookup": "hit" if hit else "miss",
                "cache_hit": hit,
                "hit": hit,
                "compile_key": key,
                "compile_key_readable": compile_key_readable(
                    model_id, version, plan["input_shapes"]
                ),
                "shape_segment": segment,
                "chain": {
                    "cache_hit": hit,
                    "compiled": not hit,
                    "first_registration_source": key_meta["first_registration_source"],
                    "registration_source": key_meta["registration_source"],
                    "key_status": key_meta["status"],
                },
                "artifact_ids": entry.artifact_ids(),
                "compile": compile_info,
                "cache_size_after": len(self.entries),
                "evictions": evictions,
                "outputs": {
                    name: tensor.to_dict() for name, tensor in specialized_outputs.items()
                },
                "baseline_outputs": baseline_result.outputs_meta,
                "baseline_full_compile_work": baseline_result.compile_work,
                "baseline_full_compile_ms": baseline_result.compile_ms,
                "numeric_comparison": comparison,
                "numeric_consistent": comparison["consistent"],
                "output_consistent": comparison["consistent"],
                "max_abs_error": comparison["max_abs_error"],
                "expected_match": expected_match,
            }
        )
        update_scope = self.last_update_scope.get(model_id)
        if update_scope is not None:
            record["model_update_context"] = {
                "event_id": update_scope["event_id"],
                "from_version": update_scope["from_version"],
                "to_version": update_scope["to_version"],
                "affected_units": list(update_scope["affected_units"]),
            }
        if not comparison["consistent"]:
            record["mismatch"] = {
                "request_id": request_id,
                "max_abs_error": comparison["max_abs_error"],
                "max_location": comparison["max_location"],
                "violations": comparison["violations"],
                "tolerance": self.tolerance,
            }
        self.request_records.append(record)
        return record

    def _make_inputs(
        self, request: Mapping[str, Any], plan: Mapping[str, Any]
    ) -> dict[str, Tensor]:
        seed_material = {
            "project_id": self.project.get("project_id"),
            "request_id": request.get("id"),
            "data_seed": request.get("data_seed"),
            "model_id": plan["model"]["model_id"],
            "model_version": plan["version"],
            "input_shapes": {
                name: list(shape) for name, shape in plan["input_shapes"].items()
            },
        }
        return {
            name: make_input_tensor(shape, {**seed_material, "tensor": name})
            for name, shape in plan["input_shapes"].items()
        }

    @staticmethod
    def _expected_match(
        expected: Mapping[str, Any], *, accepted: bool, error_code: str | None
    ) -> bool:
        if accepted:
            return bool(expected.get("valid", True))
        return (not expected.get("valid", True)) and expected.get("error_code") == error_code

    # ------------------------------------------------------------------
    # Key registration, eviction and artifact lifetime
    # ------------------------------------------------------------------
    def _register_key(
        self,
        key: str,
        plan: Mapping[str, Any],
        *,
        source: str,
        artifact_ids: Mapping[str, str],
        compile_ms: float,
        compile_work: int,
        at_time: float,
        request_id: str | None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        model_id = str(plan["model"]["model_id"])
        version = str(plan["version"])
        existing = self.key_registry.get(key)
        repeatable = True
        if existing is None:
            self.key_registry[key] = {
                "compile_key": key,
                "compile_key_readable": compile_key_readable(
                    model_id, version, plan["input_shapes"]
                ),
                "model_id": model_id,
                "model_version": version,
                "input_shapes": {
                    name: list(shape) for name, shape in plan["input_shapes"].items()
                },
                "shape_segment": shape_segment(plan["graph"], plan["input_shapes"]),
                "first_seen_request": request_id,
                "first_registration_source": source,
                "first_registered_at": at_time,
                "first_event_id": event_id,
                "registration_source": source,
                "last_registered_at": at_time,
                "times_registered": 0,
                "times_compiled": 0,
                "times_hit": 0,
                "request_occurrences": 0,
                "status": "resident",
                "first_artifact_ids": dict(artifact_ids),
                "last_artifact_ids": dict(artifact_ids),
                "artifact_ids_repeatable": True,
                "total_compile_ms": 0.0,
                "total_compile_work": 0,
                "eviction_count": 0,
            }
            existing = self.key_registry[key]
        else:
            previous_ids = existing.get("last_artifact_ids")
            if previous_ids and dict(previous_ids) != dict(artifact_ids):
                repeatable = False
                existing["artifact_ids_repeatable"] = False
        existing["registration_source"] = source
        existing["last_registered_at"] = at_time
        existing["times_registered"] = int(existing.get("times_registered", 0)) + 1
        existing["times_compiled"] = int(existing.get("times_compiled", 0)) + 1
        existing["last_artifact_ids"] = dict(artifact_ids)
        existing["last_compile_ms"] = float(compile_ms)
        existing["total_compile_ms"] = round(
            float(existing.get("total_compile_ms", 0.0)) + float(compile_ms), 6
        )
        existing["total_compile_work"] = int(existing.get("total_compile_work", 0)) + int(
            compile_work
        )
        existing["status"] = "resident"
        if event_id is not None:
            existing["last_event_id"] = event_id
        return existing

    def _referenced_artifact_ids(self) -> set[str]:
        result: set[str] = set()
        for entry in self.entries.values():
            for artifact in entry.artifacts.values():
                result.add(str(artifact["artifact_id"]))
        return result

    def _enforce_capacity(self, *, at_time: float, request_id: str | None) -> list[dict[str, Any]]:
        evictions: list[dict[str, Any]] = []
        while len(self.entries) > self.capacity:
            key, entry = self.entries.popitem(last=False)
            referenced = self._referenced_artifact_ids()
            released = []
            key_scope = []
            for unit_id, artifact in sorted(entry.artifacts.items()):
                artifact_id = str(artifact["artifact_id"])
                is_released = artifact_id not in referenced
                key_scope.append(
                    {
                        "unit_id": unit_id,
                        "scope": artifact.get("scope"),
                        "artifact_id": artifact_id,
                        "output_shape": artifact.get("output_shape"),
                        "released": is_released,
                        "shared_with_other_keys": not is_released,
                    }
                )
                if is_released and artifact_id in self.artifact_registry:
                    del self.artifact_registry[artifact_id]
                    released.append(
                        {
                            "unit_id": unit_id,
                            "scope": artifact.get("scope"),
                            "artifact_id": artifact_id,
                            "output_shape": artifact.get("output_shape"),
                        }
                    )
            meta = self.key_registry.get(key)
            if meta is not None:
                meta["status"] = "evicted"
                meta["eviction_count"] = int(meta.get("eviction_count", 0)) + 1
            eviction = {
                "eviction_id": f"evict-{len(self.eviction_log) + 1:04d}",
                "at_time": at_time,
                "request_id": request_id,
                "compile_key": key,
                "evicted_compile_key": key,
                "model_id": entry.model_id,
                "model_version": entry.model_version,
                "shape_segment": entry.shape_segment,
                "key_subgraph_scope": key_scope,
                "released_subgraph_scope": released,
                "released_unit_ids": [item["unit_id"] for item in released],
                "freed_artifact_ids": [item["artifact_id"] for item in released],
                "cache_size_after": len(self.entries),
                "policy": "LRU",
            }
            self.eviction_log.append(eviction)
            self.stats["evictions"] += 1
            evictions.append(eviction)
        return evictions

    def _purge_unreferenced(
        self, candidate_artifact_ids: Iterable[str]
    ) -> list[dict[str, Any]]:
        referenced = self._referenced_artifact_ids()
        released = []
        for artifact_id in sorted(set(candidate_artifact_ids)):
            if artifact_id in referenced:
                continue
            artifact = self.artifact_registry.pop(artifact_id, None)
            if artifact is not None:
                released.append(
                    {
                        "unit_id": artifact["unit_id"],
                        "scope": artifact.get("scope"),
                        "artifact_id": artifact_id,
                    }
                )
        return released

    # ------------------------------------------------------------------
    # Model update events
    # ------------------------------------------------------------------
    def apply_event(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self.seq += 1
        if not isinstance(event, Mapping):
            report = {
                "kind": "event",
                "event_id": f"event-{self.seq}",
                "event_type": None,
                "time": 0,
                "status": "rejected",
                "error_code": E_EVENT_INVALID,
                "error": BusinessError(E_EVENT_INVALID, "event must be an object").to_dict(),
            }
            self.event_log.append(report)
            return report
        event_id = str(event.get("id", f"event-{self.seq}"))
        event_time = float(event.get("time", 0.0))
        base = {
            "kind": "event",
            "event_id": event_id,
            "event_type": event.get("type"),
            "time": event_time,
            "model_id": event.get("model_id"),
        }
        if event.get("type") != "model_update":
            report = {
                **base,
                "status": "rejected",
                "error_code": E_EVENT_INVALID,
                "error": BusinessError(
                    E_EVENT_INVALID, f"unsupported event type {event.get('type')!r}"
                ).to_dict(),
            }
            self.event_log.append(report)
            return report
        model_id = event.get("model_id")
        from_version = event.get("from_version")
        to_version = event.get("to_version")
        model = get_model(self.project, str(model_id)) if isinstance(model_id, str) else None
        if model is None:
            report = {
                **base,
                "status": "rejected",
                "error_code": E_MODEL_NOT_FOUND,
                "error": BusinessError(E_MODEL_NOT_FOUND, f"unknown model {model_id!r}").to_dict(),
            }
            self.event_log.append(report)
            return report
        if not isinstance(from_version, str) or get_version_spec(model, from_version) is None:
            report = {
                **base,
                "status": "rejected",
                "error_code": E_VERSION_NOT_FOUND,
                "error": BusinessError(E_VERSION_NOT_FOUND, f"unknown from_version {from_version!r}").to_dict(),
            }
            self.event_log.append(report)
            return report
        if not isinstance(to_version, str) or get_version_spec(model, to_version) is None:
            report = {
                **base,
                "status": "rejected",
                "error_code": E_VERSION_NOT_FOUND,
                "error": BusinessError(E_VERSION_NOT_FOUND, f"unknown to_version {to_version!r}").to_dict(),
            }
            self.event_log.append(report)
            return report
        current = self.current_versions.get(str(model_id))
        if current != from_version:
            report = {
                **base,
                "status": "rejected",
                "error_code": E_STALE_VERSION,
                "error": BusinessError(
                    E_STALE_VERSION,
                    f"event from_version {from_version!r} does not match runtime version {current!r}",
                ).to_dict(),
            }
            self.event_log.append(report)
            return report

        diff = version_changed_units(self.project, str(model_id), from_version, to_version)
        old_entries = [
            entry for entry in self.entries.values() if entry.model_id == str(model_id)
        ]
        other_models_before = self._key_counts_by_model()
        invalidated_keys = [entry.compile_key for entry in old_entries]
        invalidated_historical_keys = [
            key
            for key, meta in self.key_registry.items()
            if meta.get("model_id") == str(model_id)
            and meta.get("model_version") == from_version
            and key not in set(invalidated_keys)
        ]
        old_artifact_ids: set[str] = set()
        for entry in old_entries:
            old_artifact_ids.update(
                str(artifact["artifact_id"]) for artifact in entry.artifacts.values()
            )
            meta = self.key_registry.get(entry.compile_key)
            if meta is not None:
                meta["status"] = "invalidated"
                meta["invalidated_by_event"] = event_id
        for key in invalidated_keys:
            self.entries.pop(key, None)
        for key in invalidated_historical_keys:
            meta = self.key_registry.get(key)
            if meta is not None:
                meta["status"] = "invalidated"
                meta["invalidated_by_event"] = event_id

        self.current_versions[str(model_id)] = to_version
        self.last_update_scope[str(model_id)] = {
            "event_id": event_id,
            "time": event_time,
            "from_version": from_version,
            "to_version": to_version,
            "changed_units": diff["changed_units"],
            "affected_units": diff["affected_units"],
            "unaffected_units": diff["unaffected_units"],
        }

        recompiled_keys: list[dict[str, Any]] = []
        stable_checks: list[bool] = []
        total_compile_work = 0
        total_compile_ms = 0.0
        compiled_units_union: set[str] = set()
        reused_unaffected_units_union: set[str] = set()
        reused_affected_units_union: set[str] = set()
        for old_entry in old_entries:
            request_like = {
                "id": f"{event_id}:recompile:{old_entry.compile_key}",
                "time": event_time,
                "model_id": str(model_id),
                "model_version": to_version,
                "input_shapes": deep_copy(old_entry.input_shapes),
                "data_seed": 0,
                "expectation": {"valid": True, "error_code": None},
            }
            plan = plan_request_graph(
                self.project,
                request_like,
                current_versions=self.current_versions,
            )
            new_key = make_compile_key(str(model_id), to_version, plan["input_shapes"])
            compile_origin = f"event:{event_id}:time={event_time:g}"
            compile_result = compile_entry_units(
                plan,
                new_key,
                artifact_registry=self.artifact_registry,
                ms_per_work_unit=self.ms_per_work_unit,
                compile_origin=compile_origin,
            )
            new_artifact_ids = {
                unit_id: artifact["artifact_id"]
                for unit_id, artifact in compile_result["artifacts"].items()
            }
            stable = all(
                unit_id in new_artifact_ids
                and unit_id in old_entry.artifacts
                and new_artifact_ids[unit_id] == old_entry.artifacts[unit_id]["artifact_id"]
                for unit_id in diff["unaffected_units"]
            )
            stable_checks.append(stable)
            reused_unaffected_for_key = [
                unit_id
                for unit_id in compile_result["reused_units"]
                if unit_id not in set(diff["affected_units"])
            ]
            reused_affected_for_key = [
                unit_id
                for unit_id in compile_result["reused_units"]
                if unit_id in set(diff["affected_units"])
            ]
            entry = CacheEntry(
                compile_key=new_key,
                model_id=str(model_id),
                model_version=to_version,
                input_shapes={
                    name: list(shape) for name, shape in plan["input_shapes"].items()
                },
                shape_segment=shape_segment(plan["graph"], plan["input_shapes"]),
                artifacts=compile_result["artifacts"],
                plan_fingerprint=plan_fingerprint(plan),
                compile_origin=compile_origin,
                created_seq=self.seq,
                last_used_seq=self.seq,
                compiled_units=compile_result["compiled_units"],
                reused_units=compile_result["reused_units"],
                unit_reports=compile_result["unit_reports"],
                shape_specialized_kernel_compiles=compile_result[
                    "shape_specialized_kernel_compiles"
                ],
                compile_work=compile_result["compile_work"],
                compile_ms=compile_result["compile_ms"],
                created_at_time=event_time,
                registration_source=compile_origin,
            )
            self.entries[new_key] = entry
            meta = self._register_key(
                new_key,
                plan,
                source=compile_origin,
                artifact_ids=entry.artifact_ids(),
                compile_ms=entry.compile_ms,
                compile_work=entry.compile_work,
                at_time=event_time,
                request_id=None,
                event_id=event_id,
            )
            self.stats["event_incremental_compiles"] += 1
            self.stats["compiled_entries"] += 1
            self.stats["compiled_units"] += len(compile_result["compiled_units"])
            self.stats["reused_units"] += len(compile_result["reused_units"])
            self.stats["shape_specialized_kernel_compiles"] += compile_result[
                "shape_specialized_kernel_compiles"
            ]
            self.stats["total_compile_work"] += int(compile_result["compile_work"])
            self.stats["total_compile_ms"] += float(compile_result["compile_ms"])
            total_compile_work += int(compile_result["compile_work"])
            total_compile_ms += float(compile_result["compile_ms"])
            compiled_units_union.update(compile_result["compiled_units"])
            for unit_id in compile_result["reused_units"]:
                if unit_id in set(diff["affected_units"]):
                    reused_affected_units_union.add(unit_id)
                else:
                    reused_unaffected_units_union.add(unit_id)
            recompiled_keys.append(
                {
                    "old_compile_key": old_entry.compile_key,
                    "new_compile_key": new_key,
                    "shape_segment": entry.shape_segment,
                    "compiled_units": list(compile_result["compiled_units"]),
                    "reused_units": list(compile_result["reused_units"]),
                    "reused_unaffected_units": reused_unaffected_for_key,
                    "reused_affected_units": reused_affected_for_key,
                    "unit_reports": deep_copy(compile_result["unit_reports"]),
                    "compile_work": compile_result["compile_work"],
                    "compile_ms": compile_result["compile_ms"],
                    "unaffected_artifact_ids_stable": stable,
                    "artifact_ids": entry.artifact_ids(),
                }
            )
        evictions = self._enforce_capacity(at_time=event_time, request_id=None)
        released = self._purge_unreferenced(old_artifact_ids)
        other_models_after = self._key_counts_by_model()
        unaffected_keys_preserved = all(
            other_models_after.get(model, 0) == count
            for model, count in other_models_before.items()
            if model != model_id
        )
        status = "applied" if old_entries else "applied_deferred"
        report = {
            **base,
            "status": status,
            "from_version": from_version,
            "to_version": to_version,
            "invalidated_keys": invalidated_keys,
            "invalidated_key_count": len(invalidated_keys),
            "invalidated_historical_keys": invalidated_historical_keys,
            "invalidated_historical_key_count": len(invalidated_historical_keys),
            "invalidated_total_key_count": len(invalidated_keys)
            + len(invalidated_historical_keys),
            "changed_units": diff["changed_units"],
            "incremental_recompile_scope": diff["affected_units"],
            "affected_units": diff["affected_units"],
            "unaffected_units": diff["unaffected_units"],
            "compiled_units_union": sorted(compiled_units_union),
            "reused_units_union": sorted(reused_unaffected_units_union),
            "reused_affected_units_union": sorted(reused_affected_units_union),
            "recompiled_keys": recompiled_keys,
            "recompiled_key_count": len(recompiled_keys),
            "unaffected_artifact_ids_stable": all(stable_checks) if stable_checks else True,
            "other_model_keys_preserved": unaffected_keys_preserved,
            "other_model_key_counts_before": other_models_before,
            "other_model_key_counts_after": other_models_after,
            "released_artifact_count": len(released),
            "released_artifacts": released,
            "evictions": evictions,
            "total_compile_work": total_compile_work,
            "total_compile_ms": round(total_compile_ms, 6),
            "deferred_recompile": not old_entries,
        }
        if not stable_checks:
            report["note"] = (
                "No cached shape existed for this model, so the incremental scope is "
                "recorded and will be applied on the next request with a concrete shape."
            )
        self.event_log.append(report)
        return report

    def _key_counts_by_model(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for model_id in self.project.get("models", {}):
            counts[str(model_id)] = 0
        for entry in self.entries.values():
            counts[entry.model_id] = counts.get(entry.model_id, 0) + 1
        return counts

    # ------------------------------------------------------------------
    # Summary and snapshots
    # ------------------------------------------------------------------
    def summary(self) -> dict[str, Any]:
        lookups = self.stats["cache_hits"] + self.stats["cache_misses"]
        hit_rate = (self.stats["cache_hits"] / lookups) if lookups else 0.0
        baseline_work = int(self.stats["baseline_compile_work"])
        specialized_work = int(self.stats["total_compile_work"])
        saved_work = max(0, baseline_work - specialized_work)
        distribution: dict[str, dict[str, Any]] = {}
        for key, meta in sorted(self.key_registry.items()):
            group = f"{meta['model_id']}@{meta['model_version']}"
            segment = meta["shape_segment"]
            bucket = distribution.setdefault(
                group,
                {
                    "model_id": meta["model_id"],
                    "model_version": meta["model_version"],
                    "keys": 0,
                    "resident_keys": 0,
                    "request_occurrences": 0,
                    "shape_segments": {},
                },
            )
            bucket["keys"] += 1
            if meta.get("status") == "resident":
                bucket["resident_keys"] += 1
            bucket["request_occurrences"] += int(meta.get("request_occurrences", 0))
            segment_bucket = bucket["shape_segments"].setdefault(
                segment, {"keys": 0, "request_occurrences": 0, "resident_keys": 0}
            )
            segment_bucket["keys"] += 1
            segment_bucket["resident_keys"] += 1 if meta.get("status") == "resident" else 0
            segment_bucket["request_occurrences"] += int(meta.get("request_occurrences", 0))

        request_key_distribution: dict[str, dict[str, Any]] = {}
        for record in self.request_records:
            if record.get("status") != "accepted":
                continue
            key = record["compile_key"]
            bucket = request_key_distribution.setdefault(
                key,
                {
                    "compile_key": key,
                    "model_id": record["model_id"],
                    "model_version": record["model_version"],
                    "shape_segment": record["shape_segment"],
                    "requests": 0,
                    "hits": 0,
                    "misses": 0,
                },
            )
            bucket["requests"] += 1
            if record["cache_hit"]:
                bucket["hits"] += 1
            else:
                bucket["misses"] += 1

        consistency = self._consistency_checks()
        return {
            "valid_requests": self.stats["valid_requests"],
            "rejected_requests": self.stats["rejected_requests"],
            "cache_hits": self.stats["cache_hits"],
            "cache_misses": self.stats["cache_misses"],
            "lookups": lookups,
            "hit_rate": round(hit_rate, 6),
            "cache_capacity": self.capacity,
            "cache_size": len(self.entries),
            "eviction_count": self.stats["evictions"],
            "shape_specialization_events": self.stats["shape_specialization_events"],
            "shape_specialized_kernel_compiles": self.stats[
                "shape_specialized_kernel_compiles"
            ],
            "request_novel_key_compiles": self.stats["request_novel_key_compiles"],
            "request_recompiles_after_eviction": self.stats[
                "request_recompiles_after_eviction"
            ],
            "event_incremental_compiles": self.stats["event_incremental_compiles"],
            "compiled_entries": self.stats["compiled_entries"],
            "compiled_units": self.stats["compiled_units"],
            "reused_units": self.stats["reused_units"],
            "total_compile_work": specialized_work,
            "total_compile_ms": round(float(self.stats["total_compile_ms"]), 6),
            "baseline_full_compile_count": self.stats["baseline_full_compile_count"],
            "baseline_compile_work": baseline_work,
            "baseline_compile_ms": round(float(self.stats["baseline_compile_ms"]), 6),
            "saved_compile_work": saved_work,
            "work_reduction_ratio": round(saved_work / baseline_work, 6)
            if baseline_work
            else 0.0,
            "consistent_requests": self.stats["consistent_requests"],
            "inconsistent_requests": self.stats["inconsistent_requests"],
            "expectation_mismatches": self.stats["expectation_mismatches"],
            "distribution_by_model_version": {
                key: distribution[key] for key in sorted(distribution)
            },
            "request_key_distribution": [
                request_key_distribution[key] for key in sorted(request_key_distribution)
            ],
            "first_registration_sources": self._source_counts(),
            "key_registry": [
                {
                    "compile_key": key,
                    "model_id": meta.get("model_id"),
                    "model_version": meta.get("model_version"),
                    "shape_segment": meta.get("shape_segment"),
                    "first_seen_request": meta.get("first_seen_request"),
                    "first_registration_source": meta.get("first_registration_source"),
                    "registration_source": meta.get("registration_source"),
                    "status": meta.get("status"),
                    "times_registered": meta.get("times_registered"),
                    "times_compiled": meta.get("times_compiled"),
                    "times_hit": meta.get("times_hit"),
                    "request_occurrences": meta.get("request_occurrences"),
                    "artifact_ids_repeatable": meta.get(
                        "artifact_ids_repeatable", True
                    ),
                    "first_artifact_ids": meta.get("first_artifact_ids"),
                    "last_artifact_ids": meta.get("last_artifact_ids"),
                }
                for key, meta in sorted(self.key_registry.items())
            ],
            "consistency": consistency,
            "evictions": self.eviction_log,
            "events": self.event_log,
        }

    def _source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for meta in self.key_registry.values():
            source = str(meta.get("first_registration_source", ""))
            prefix = source.split(":", 1)[0] if source else "unknown"
            counts[prefix] = counts.get(prefix, 0) + 1
        return counts

    def _consistency_checks(self) -> dict[str, Any]:
        accepted = [record for record in self.request_records if record.get("status") == "accepted"]
        shape_groups: dict[tuple[Any, ...], set[str]] = {}
        version_groups: dict[tuple[Any, ...], set[str]] = {}
        for record in accepted:
            shapes = tuple(
                (name, tuple(int(dim) for dim in shape))
                for name, shape in sorted(record["input_shapes"].items())
            )
            shape_groups.setdefault((record["model_id"], record["model_version"], shapes), set()).add(
                record["compile_key"]
            )
            version_groups.setdefault((record["model_id"], shapes), set()).add(
                record["compile_key"]
            )
        same_shape_same_key = all(len(keys) == 1 for keys in shape_groups.values())
        different_version_diff_key = all(len(keys) >= 1 for keys in version_groups.values())
        # For models with more than one version in the sequence, verify equal
        # shapes across versions produce distinct keys.
        version_pairs_ok = True
        per_model_versions: dict[str, set[str]] = {}
        for model_id, version, shapes in shape_groups:
            per_model_versions.setdefault(model_id, set()).add(version)
        for model_id, versions in per_model_versions.items():
            if len(versions) < 2:
                continue
            for shapes in {item[2] for item in shape_groups if item[0] == model_id}:
                keys = {
                    next(iter(shape_groups[(model_id, version, shapes)]))
                    for version in versions
                    if (model_id, version, shapes) in shape_groups
                }
                if len(keys) > 1:
                    version_pairs_ok = True
                elif len(keys) == 1 and len(
                    [v for v in versions if (model_id, v, shapes) in shape_groups]
                ) > 1:
                    version_pairs_ok = False
        request_keys = {record["compile_key"] for record in accepted}
        registered_request_keys = {
            key for key in request_keys if key in self.key_registry
        }
        repeatable = all(
            meta.get("artifact_ids_repeatable", True) for meta in self.key_registry.values()
        )
        return {
            "same_version_same_shape_same_key": same_shape_same_key,
            "all_request_keys_registered": len(request_keys) == len(registered_request_keys),
            "request_unique_key_count": len(request_keys),
            "registered_request_key_count": len(registered_request_keys),
            "different_version_produces_distinct_key": version_pairs_ok,
            "artifact_ids_repeatable_after_eviction": repeatable,
            "all_numeric_outputs_consistent": self.stats["inconsistent_requests"] == 0,
            "expectations_match": self.stats["expectation_mismatches"] == 0,
        }

    def snapshot_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "snapshot_schema": SNAPSHOT_SCHEMA,
            "project_id": self.project.get("project_id"),
            "project_fingerprint": project_fingerprint(self.project),
            "model_graph_fingerprints": model_graph_fingerprints(self.project),
            "runtime_model_versions": dict(sorted(self.current_versions.items())),
            "cache_config": dict(self.config),
            "capacity": self.capacity,
            "entries": [entry.to_dict() for entry in self.entries.values()],
            "lru_order": list(self.entries.keys()),
            "artifact_registry": deep_copy(self.artifact_registry),
            "key_registry": deep_copy(self.key_registry),
            "stats": deep_copy(self.stats),
            "seq": self.seq,
            "eviction_log": deep_copy(self.eviction_log),
            "event_log": deep_copy(self.event_log),
            "last_update_scope": deep_copy(self.last_update_scope),
            "baseline_state": {
                "compile_count": self.baseline.compile_count,
                "total_compile_work": self.baseline.total_compile_work,
                "total_compile_ms": self.baseline.total_compile_ms,
            },
        }
        snapshot_id = "snap_" + sha256_hex(payload, length=24)
        payload["snapshot_id"] = snapshot_id
        return payload

    def write_snapshot(self, path: str | Path) -> dict[str, Any]:
        payload = self.snapshot_payload()
        atomic_write_json(path, payload)
        return payload

    @classmethod
    def restore(
        cls,
        path: str | Path,
        project: Mapping[str, Any],
        *,
        strict_snapshot_id: bool = True,
    ) -> "CompileCacheEngine":
        data = read_json(path)
        if not isinstance(data, Mapping):
            raise BusinessRuleError(E_SNAPSHOT_SCHEMA, "snapshot must be a JSON object")
        if data.get("snapshot_schema") != SNAPSHOT_SCHEMA:
            raise BusinessRuleError(
                E_SNAPSHOT_SCHEMA,
                f"snapshot schema {data.get('snapshot_schema')!r} is not supported",
            )
        expected_id = "snap_" + sha256_hex(
            {key: value for key, value in data.items() if key != "snapshot_id"},
            length=24,
        )
        snapshot_id_ok = data.get("snapshot_id") == expected_id
        if strict_snapshot_id and not snapshot_id_ok:
            raise BusinessRuleError(
                E_SNAPSHOT_INTEGRITY,
                "snapshot id does not match snapshot contents",
                context={"stored": data.get("snapshot_id"), "computed": expected_id},
            )
        engine = cls(
            project,
            cache_config_override=data.get("cache_config", {}),
        )
        engine.seq = int(data.get("seq", 0))
        engine.key_registry = deep_copy(data.get("key_registry", {}))
        engine.artifact_registry = deep_copy(data.get("artifact_registry", {}))
        engine.stats = deep_copy(data.get("stats", engine.stats))
        engine.eviction_log = deep_copy(data.get("eviction_log", []))
        engine.event_log = deep_copy(data.get("event_log", []))
        engine.last_update_scope = deep_copy(data.get("last_update_scope", {}))
        baseline_state = data.get("baseline_state", {})
        engine.baseline.compile_count = int(baseline_state.get("compile_count", 0))
        engine.baseline.total_compile_work = int(baseline_state.get("total_compile_work", 0))
        engine.baseline.total_compile_ms = float(baseline_state.get("total_compile_ms", 0.0))

        project_fps = model_graph_fingerprints(project)
        snapshot_fps = data.get("model_graph_fingerprints", {})
        project_id_match = data.get("project_id") == project.get("project_id")
        project_fingerprint_match = data.get("project_fingerprint") == project_fingerprint(project)
        snapshot_cache_config = data.get("cache_config", {})
        project_cache_config = project.get("cache_config", {})
        cache_config_match = dict(snapshot_cache_config) == dict(project_cache_config)
        version_changes: list[dict[str, Any]] = []
        invalidated_keys: list[str] = []
        valid_entries: list[CacheEntry] = []
        entries_raw = data.get("entries", [])
        if not isinstance(entries_raw, list):
            raise BusinessRuleError(E_SNAPSHOT_SCHEMA, "snapshot entries must be a list")
        for raw in entries_raw:
            entry = CacheEntry.from_dict(raw)
            model_versions = project_fps.get(entry.model_id)
            reason = None
            if model_versions is None:
                reason = "model_removed"
            elif entry.model_version not in model_versions:
                reason = "version_removed"
            else:
                previous = snapshot_fps.get(entry.model_id, {}).get(entry.model_version)
                current = model_versions[entry.model_version]
                if previous is not None and previous != current:
                    reason = "graph_fingerprint_changed"
            if reason is not None:
                version_changes.append(
                    {
                        "compile_key": entry.compile_key,
                        "model_id": entry.model_id,
                        "model_version": entry.model_version,
                        "reason": reason,
                        "code": E_SNAPSHOT_VERSION_CHANGED,
                    }
                )
                invalidated_keys.append(entry.compile_key)
                meta = engine.key_registry.get(entry.compile_key)
                if meta is not None:
                    meta["status"] = "invalidated"
                    meta["invalidated_by_snapshot"] = reason
                continue
            valid_entries.append(entry)

        lru_order = [
            key
            for key in data.get("lru_order", [])
            if key not in set(invalidated_keys)
        ]
        by_key = {entry.compile_key: entry for entry in valid_entries}
        for key in lru_order:
            entry = by_key.get(key)
            if entry is not None and key not in engine.entries:
                engine.entries[key] = entry
        for entry in sorted(valid_entries, key=lambda item: item.last_used_seq):
            if entry.compile_key not in engine.entries:
                engine.entries[entry.compile_key] = entry

        runtime_versions_raw = data.get("runtime_model_versions", {})
        engine.current_versions = {
            model_id: str(model["initial_version"])
            for model_id, model in project.get("models", {}).items()
        }
        for model_id, version in runtime_versions_raw.items():
            model = get_model(project, str(model_id))
            if model is not None and isinstance(version, str) and get_version_spec(model, version) is not None:
                engine.current_versions[str(model_id)] = version
            else:
                version_changes.append(
                    {
                        "model_id": model_id,
                        "model_version": version,
                        "reason": "runtime_version_unavailable",
                        "code": E_SNAPSHOT_VERSION_CHANGED,
                    }
                )
        engine._rebuild_artifact_registry_from_entries()
        engine.restore_report = {
            "snapshot_schema_ok": True,
            "snapshot_id_ok": snapshot_id_ok,
            "project_id_match": project_id_match,
            "project_fingerprint_match": project_fingerprint_match,
            "cache_config_match": cache_config_match,
            "snapshot_cache_config": deep_copy(snapshot_cache_config),
            "project_cache_config": deep_copy(project_cache_config),
            "entries_loaded": len(engine.entries),
            "artifacts_loaded": len(engine.artifact_registry),
            "invalidated_keys": invalidated_keys,
            "version_changes": version_changes,
            "version_change_detected": bool(version_changes),
            "full_compilations_during_restore": 0,
            "restored_from_snapshot_id": data.get("snapshot_id"),
            "runtime_model_versions": dict(engine.current_versions),
            "warnings": [],
        }
        if not project_id_match:
            engine.restore_report["warnings"].append(
                {
                    "code": E_SNAPSHOT_PROJECT_MISMATCH,
                    "message": "snapshot project_id differs from the supplied project",
                }
            )
        if not cache_config_match:
            engine.restore_report["warnings"].append(
                {
                    "code": E_SNAPSHOT_PROJECT_MISMATCH,
                    "message": "snapshot cache_config differs from the supplied project; snapshot config was used",
                }
            )
        return engine

    def _rebuild_artifact_registry_from_entries(self) -> None:
        registry: dict[str, dict[str, Any]] = {}
        for entry in self.entries.values():
            for artifact in entry.artifacts.values():
                registry[str(artifact["artifact_id"])] = artifact
        self.artifact_registry = registry
