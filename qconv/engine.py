"""Application engine: graph editing, scheduling, caching and recovery."""

from __future__ import annotations

import copy
import json
import math
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from .cache import ArtifactCache, CacheArtifact
from .config import (
    InjectionConfig,
    InjectionItem,
    ScheduleConfig,
    config_hash,
    load_injection_config,
    load_schedule_config,
)
from .conv import (
    IMPLEMENTATION_VERSION,
    ActivationSource,
    ConvResult,
    RuntimeNodeParams,
    compute_node,
    expected_macs,
    runtime_params,
)
from .errors import (
    BudgetExhaustedError,
    ConfigMismatchError,
    DependencyFailedError,
    Failure,
    GraphInvalidError,
    NodeExecutionError,
    PackInvalidError,
    QConvError,
    QuantParamError,
    SnapshotInvalidError,
)
from .graph import (
    QUANT_CODE,
    STRUCTURAL_CODE,
    Graph,
    QuantNode,
    load_graph,
    normalized_graph_from_dict,
)
from .packs import PackRepository, PackTensor, load_pack_repository
from .snapshot import SessionStore
from .util import (
    atomic_write_json,
    atomic_write_npy,
    sha256_json,
)


def _json_sanitizable(value: Any) -> Any:
    """Replace non-finite floats before building a deterministic cache key."""

    if isinstance(value, float):
        if math.isnan(value):
            return "non-finite:nan"
        if math.isinf(value):
            return "non-finite:inf" if value > 0 else "non-finite:-inf"
        return value
    if isinstance(value, dict):
        return {str(key): _json_sanitizable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_sanitizable(item) for item in value]
    return value


@dataclass
class TaskSpec:
    node_id: str
    sort_key: str
    node: QuantNode
    source: ActivationSource
    weight_pack: PackTensor
    params: RuntimeNodeParams
    cache_key: str
    requested_macs: int
    injection_items: tuple[InjectionItem, ...]


@dataclass
class TaskOutcome:
    task: TaskSpec
    success: bool
    result: ConvResult | None = None
    failure: Failure | None = None
    elapsed_ms: float = 0.0
    injection_hits: list[dict[str, Any]] = field(default_factory=list)
    executed_macs: int = 0


class QConvEngine:
    """Stateful workbench for deterministic quantized convolution graphs."""

    def __init__(
        self,
        graph_path: str | Path,
        pack_manifest_path: str | Path,
        schedule_path: str | Path | ScheduleConfig,
        session_dir: str | Path,
        injection_path: str | Path | InjectionConfig | None = None,
        *,
        resume_working_graph: bool = True,
    ) -> None:
        self.graph_path = Path(graph_path)
        self.pack_manifest_path = Path(pack_manifest_path)
        if isinstance(schedule_path, ScheduleConfig):
            self.schedule = schedule_path
        else:
            self.schedule = load_schedule_config(schedule_path)
        if isinstance(injection_path, InjectionConfig):
            self.injection = injection_path
        elif injection_path is None:
            self.injection = InjectionConfig()
        else:
            self.injection = load_injection_config(injection_path)
        self.config_hash = config_hash(self.schedule, self.injection)
        self.quantization_enabled = self.schedule.quantization_enabled

        self.session = SessionStore(session_dir)
        self.snapshot_error: Failure | None = None
        try:
            loaded_snapshot = self.session.loaded_snapshot
        except SnapshotInvalidError as exc:
            loaded_snapshot = None
            self.snapshot_error = exc.to_failure()
        self.loaded_snapshot = (
            copy.deepcopy(loaded_snapshot.data) if loaded_snapshot is not None else None
        )

        source_result = load_graph(self.graph_path)
        self.graph_source_hash = source_result.source_hash
        self.load_failures: list[Failure] = list(source_result.failures)
        self.node_parameter_failures: dict[str, list[Failure]] = {
            key: list(value) for key, value in source_result.node_parameter_failures.items()
        }
        self.graph: Graph | None = source_result.graph
        self.fatal = source_result.fatal
        self.working_graph_loaded = False

        working = self.session.read_working_graph() if resume_working_graph else None
        if (
            working is not None
            and not self.fatal
            and working.get("base_graph_hash") == self.graph_source_hash
        ):
            try:
                candidate = normalized_graph_from_dict(working["graph"])
                candidate.refresh_topology()
                if len(candidate.topology) == len(candidate.nodes):
                    self.graph = candidate
                    self.working_graph_loaded = True
                    runtime = working.get("runtime", {})
                    if isinstance(runtime, dict) and "quantization_enabled" in runtime:
                        self.quantization_enabled = bool(runtime["quantization_enabled"])
            except (KeyError, TypeError, ValueError):
                self.working_graph_loaded = False

        self.pack_repository: PackRepository | None = None
        if self.graph is not None and not self.fatal:
            try:
                self.pack_repository = load_pack_repository(self.pack_manifest_path, self.graph)
            except QConvError as exc:
                self.load_failures.append(exc.to_failure())
                self.fatal = True
            except Exception as exc:  # defensive isolation at the application boundary
                self.load_failures.append(
                    Failure(
                        code="E_PACK_INVALID",
                        message=f"cannot load quantized packs: {exc}",
                        stage="pack_load",
                        exception_type=type(exc).__name__,
                    )
                )
                self.fatal = True
        if self.snapshot_error is not None:
            self.load_failures.append(self.snapshot_error)
            self.fatal = True

        self.pending_invalidations: set[str] = set()
        self.last_results: dict[str, ConvResult] = {}
        self.last_report: dict[str, Any] | None = None

    # ------------------------------------------------------------------
    # Editing operations
    # ------------------------------------------------------------------
    def _require_graph(self) -> Graph:
        if self.graph is None or self.fatal:
            raise GraphInvalidError("graph is not editable because loading failed")
        return self.graph

    def _mark_affected(self, node_id: str) -> set[str]:
        graph = self._require_graph()
        affected = {node_id} | graph.descendants([node_id])
        self.pending_invalidations.update(affected)
        return affected

    def _persist_working_graph(self) -> None:
        graph = self._require_graph()
        self.session.write_working_graph(
            self.graph_source_hash,
            graph.to_dict(),
            runtime={"quantization_enabled": self.quantization_enabled},
        )

    def set_bit_width(self, node_id: str, target: str, bit_width: int) -> list[str]:
        graph = self._require_graph()
        if node_id not in graph.nodes:
            raise GraphInvalidError(f"unknown node id: {node_id}", node_id=node_id)
        if target not in {"weight", "activation"}:
            raise QuantParamError("target must be weight or activation", node_id=node_id)
        if isinstance(bit_width, bool) or not isinstance(bit_width, int) or not 2 <= bit_width <= 16:
            raise QuantParamError(
                f"bit width must be an integer in [2, 16], got {bit_width!r}", node_id=node_id
            )
        affected = self._mark_affected(node_id)
        node = graph.nodes[node_id]
        if target == "weight":
            node.weight_bit_width = bit_width
        else:
            node.activation_bit_width = bit_width
        graph.graph_version += 1
        graph.refresh_topology()
        self._persist_working_graph()
        return graph.sorted_nodes(affected)

    def set_scale(self, node_id: str, target: str, scale: float) -> list[str]:
        graph = self._require_graph()
        if node_id not in graph.nodes:
            raise GraphInvalidError(f"unknown node id: {node_id}", node_id=node_id)
        if target not in {"weight", "activation"}:
            raise QuantParamError("target must be weight or activation", node_id=node_id)
        if not math.isfinite(float(scale)) or float(scale) <= 0.0:
            raise QuantParamError(
                f"scale must be finite and positive, got {scale!r}", node_id=node_id
            )
        affected = self._mark_affected(node_id)
        node = graph.nodes[node_id]
        if target == "weight":
            node.weight_scale = float(scale)
        else:
            node.activation_scale = float(scale)
        graph.graph_version += 1
        graph.refresh_topology()
        self._persist_working_graph()
        return graph.sorted_nodes(affected)

    def set_quantization_enabled(self, enabled: bool) -> list[str]:
        graph = self._require_graph()
        self.quantization_enabled = bool(enabled)
        affected = set(graph.nodes)
        self.pending_invalidations.update(affected)
        self._persist_working_graph()
        return graph.sorted_nodes(affected)

    def replace_subgraph(self, target_id: str, patch: dict[str, Any]) -> list[str]:
        """Replace ``target_id`` and all descendants with a patch subgraph.

        Patch keys are ``nodes``, ``edges`` and optional ``outputs``.  New nodes
        use the same node schema as the source graph and may reference existing
        manifest weight-pack keys.
        """

        graph = self._require_graph()
        if target_id not in graph.nodes:
            raise GraphInvalidError(f"unknown subgraph target: {target_id}", node_id=target_id)
        if not isinstance(patch, dict):
            raise GraphInvalidError("subgraph patch must be an object")
        patch_nodes = patch.get("nodes")
        patch_edges = patch.get("edges")
        if not isinstance(patch_nodes, list) or not patch_nodes:
            raise GraphInvalidError("subgraph patch requires a non-empty nodes array")
        if not isinstance(patch_edges, list):
            raise GraphInvalidError("subgraph patch requires an edges array")

        closure = {target_id} | graph.descendants([target_id])
        remaining_ids = [node_id for node_id in graph.topology if node_id not in closure]
        remaining_id_set = set(remaining_ids)
        new_ids: set[str] = set()
        for raw in patch_nodes:
            if not isinstance(raw, dict) or not isinstance(raw.get("id"), str) or not raw["id"]:
                raise GraphInvalidError("every patch node requires a non-empty string id")
            new_ids.add(raw["id"])
        if new_ids & remaining_id_set:
            raise GraphInvalidError("patch node id collides with an unaffected node")
        remaining_outputs = {
            tensor
            for tensor in [graph.input.name] + [graph.nodes[node_id].output_tensor for node_id in remaining_ids]
        }
        preserve_edges: list[dict[str, str]] = []
        for edge in graph.edges:
            if edge.destination in closure:
                continue
            if edge.source not in remaining_outputs:
                raise GraphInvalidError(
                    f"unaffected edge {edge.source}->{edge.destination} depends on replaced subgraph"
                )
            preserve_edges.append(edge.to_dict())

        if "outputs" in patch:
            outputs = patch["outputs"]
            if not isinstance(outputs, list) or any(not isinstance(item, str) for item in outputs):
                raise GraphInvalidError("patch outputs must be an array of tensor names")
        else:
            destinations = {
                item.get("to") for item in patch_edges if isinstance(item, dict) and isinstance(item.get("to"), str)
            }
            terminal_new = [
                raw["id"]
                for raw in patch_nodes
                if isinstance(raw.get("id"), str) and raw["id"] not in destinations
            ]
            preserved = [tensor for tensor in graph.outputs if tensor in remaining_outputs]
            outputs = preserved + terminal_new
            outputs = list(dict.fromkeys(outputs))

        raw_graph = {
            "schema_version": graph.schema_version,
            "graph_version": graph.graph_version + 1,
            "input": graph.input.to_dict(),
            "nodes": [graph.nodes[node_id].to_dict() for node_id in remaining_ids] + patch_nodes,
            "edges": preserve_edges + patch_edges,
            "outputs": outputs,
        }
        fd, temp_name = tempfile.mkstemp(prefix=".subgraph-patch-", suffix=".json")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(raw_graph, handle)
            validation = load_graph(temp_name)
        finally:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass
        if validation.graph is None or validation.fatal:
            messages = "; ".join(failure.message for failure in validation.failures)
            raise GraphInvalidError(f"replacement graph is invalid: {messages}")
        quant_failures = [failure for failure in validation.failures if failure.code == QUANT_CODE]
        if quant_failures:
            raise QuantParamError("; ".join(failure.message for failure in quant_failures))
        new_graph = validation.graph
        new_graph.graph_version = graph.graph_version + 1
        new_graph.refresh_topology()
        try:
            new_packs = load_pack_repository(self.pack_manifest_path, new_graph)
        except QConvError as exc:
            raise GraphInvalidError(f"replacement pack validation failed: {exc.message}") from exc

        self.graph = new_graph
        self.pack_repository = new_packs
        self.last_results = {
            node_id: result for node_id, result in self.last_results.items() if node_id in new_graph.nodes
        }
        self.pending_invalidations = {
            node_id for node_id in self.pending_invalidations if node_id in new_graph.nodes
        }
        affected = set(new_ids)
        self.pending_invalidations.update(affected)
        self._persist_working_graph()
        return new_graph.sorted_nodes(affected | (closure & set(new_graph.nodes)))

    def save_graph(self, path: str | Path) -> None:
        graph = self._require_graph()
        atomic_write_json(Path(path), graph.to_dict())

    def export_outputs(self, directory: str | Path) -> dict[str, Any]:
        destination = Path(directory)
        destination.mkdir(parents=True, exist_ok=True)
        manifest: dict[str, Any] = {"nodes": {}}
        for node_id, result in self.last_results.items():
            filename = f"{node_id}.npy"
            atomic_write_npy(destination / filename, result.output)
            manifest["nodes"][node_id] = {
                "file": filename,
                "shape": list(result.output.shape),
                "output_digest": result.output_digest,
                "integer_digest": result.integer_digest,
            }
        atomic_write_json(destination / "outputs.json", manifest)
        return manifest

    # ------------------------------------------------------------------
    # Run helpers
    # ------------------------------------------------------------------
    def _node_invalid_from_load(self, node_id: str) -> bool:
        return bool(self.node_parameter_failures.get(node_id))

    def _graph_scalar_required_macs(self, node: QuantNode) -> int:
        if self.graph is None:
            return 0
        shape = self.graph.output_shapes.get(node.input_tensor)
        if shape is None:
            return 0
        return expected_macs(node, shape)

    def _producer_node_id(self, tensor_name: str) -> str | None:
        if self.graph is None:
            return None
        for node in self.graph.nodes.values():
            if node.output_tensor == tensor_name:
                return node.id
        return None

    def _make_source(self, node: QuantNode) -> ActivationSource:
        graph = self._require_graph()
        if node.input_tensor == graph.input.name:
            if self.pack_repository is None:
                raise PackInvalidError("pack repository is unavailable")
            pack = self.pack_repository.input_tensor
            descriptor = pack.descriptor
            return ActivationSource(
                kind="root",
                integer=pack.values,
                output=None,
                source_scale=float(
                    descriptor.scale if descriptor.scale is not None else graph.input.scale
                ),
                source_bit_width=int(
                    descriptor.bit_width
                    if descriptor.bit_width is not None
                    else graph.input.bit_width
                ),
                source_digest=pack.content_digest,
            )
        producer_id = self._producer_node_id(node.input_tensor)
        if producer_id is None:
            raise DependencyFailedError(
                f"input tensor {node.input_tensor!r} has no computed producer", node_id=node.id
            )
        upstream = self.last_results.get(producer_id)
        if upstream is None:
            raise DependencyFailedError(
                f"producer node {producer_id} has no available output", node_id=node.id
            )
        return ActivationSource(
            kind="tensor",
            integer=upstream.integer,
            output=upstream.output,
            source_scale=1.0,
            source_bit_width=16,
            source_digest=(upstream.output_digest if self.quantization_enabled else upstream.integer_digest),
            producer_id=producer_id,
        )

    def _cache_key(
        self,
        node: QuantNode,
        source: ActivationSource,
        params: RuntimeNodeParams,
        weight_pack: PackTensor,
        injection_items: tuple[InjectionItem, ...],
    ) -> str:
        payload = {
            "implementation_version": IMPLEMENTATION_VERSION,
            "node_id": node.id,
            "node": node.cache_parameters(),
            "effective_params": {
                "weight_bit_width": params.weight_bit_width,
                "weight_scale": params.weight_scale,
                "activation_bit_width": params.activation_bit_width,
                "activation_scale": params.activation_scale,
                "accumulator_bits": params.accumulator_bits,
                "overflow_policy": params.overflow_policy,
            },
            "weight": {
                "file_digest": weight_pack.file_digest,
                "content_digest": weight_pack.content_digest,
                "source_scale": weight_pack.descriptor.scale,
                "source_bit_width": weight_pack.descriptor.bit_width,
                "shape": list(weight_pack.values.shape),
            },
            "activation": {
                "kind": source.kind,
                "digest": source.source_digest,
                "source_scale": source.source_scale,
                "source_bit_width": source.source_bit_width,
            },
            "quantization_enabled": self.quantization_enabled,
            "rounding_mode": self.schedule.rounding_mode,
            "dequant_decimals": self.schedule.dequant_decimals,
            "injection": [item.to_dict() for item in injection_items],
        }
        return sha256_json(_json_sanitizable(payload))

    def _params_and_injections(
        self, node: QuantNode, items: tuple[InjectionItem, ...]
    ) -> tuple[RuntimeNodeParams, list[dict[str, Any]]]:
        values = runtime_params(node).__dict__.copy()
        hits: list[dict[str, Any]] = []
        for item in items:
            if item.type == "non_finite_scale":
                target = item.target or "weight"
                values[f"{target}_scale"] = float("nan")
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": node.id,
                        "target": target,
                        "stage": "parameter_construction",
                        "effect": f"{target}_scale=nan",
                    }
                )
            elif item.type == "bit_width_out_of_range":
                target = item.target or "weight"
                width = int(item.value) if item.value is not None else 17
                values[f"{target}_bit_width"] = width
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": node.id,
                        "target": target,
                        "stage": "parameter_construction",
                        "effect": f"{target}_bit_width={width}",
                    }
                )
            elif item.type == "accumulator_overflow":
                width = int(item.value) if item.value is not None else 4
                values["accumulator_bits"] = width
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": node.id,
                        "stage": "integer_conv",
                        "effect": f"accumulator_bits={width}",
                    }
                )
            elif item.type == "node_exception":
                stage = str(item.value) if item.value is not None else "integer_conv"
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": node.id,
                        "stage": stage,
                        "effect": "synthetic node exception",
                    }
                )
        return (
            RuntimeNodeParams(
                weight_bit_width=values["weight_bit_width"],
                weight_scale=values["weight_scale"],
                activation_bit_width=values["activation_bit_width"],
                activation_scale=values["activation_scale"],
                accumulator_bits=values["accumulator_bits"],
                overflow_policy=values["overflow_policy"],
            ),
            hits,
        )

    def _execute_task(self, task: TaskSpec) -> TaskOutcome:
        started = time.perf_counter()
        hits: list[dict[str, Any]] = []
        for item in task.injection_items:
            target = item.target or "weight"
            if item.type == "non_finite_scale":
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": task.node_id,
                        "target": target,
                        "stage": "parameter_construction",
                        "effect": f"{target}_scale=nan",
                    }
                )
            elif item.type == "bit_width_out_of_range":
                width = int(item.value) if item.value is not None else 17
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": task.node_id,
                        "target": target,
                        "stage": "parameter_construction",
                        "effect": f"{target}_bit_width={width}",
                    }
                )
            elif item.type == "accumulator_overflow":
                width = int(item.value) if item.value is not None else 4
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": task.node_id,
                        "stage": "integer_conv",
                        "effect": f"accumulator_bits={width}",
                    }
                )
            elif item.type == "node_exception":
                hits.append(
                    {
                        "id": item.id,
                        "type": item.type,
                        "node_id": task.node_id,
                        "stage": str(item.value or "integer_conv"),
                        "effect": "synthetic node exception",
                    }
                )
        try:
            exception_item = next(
                (item for item in task.injection_items if item.type == "node_exception"), None
            )
            if exception_item is not None:
                stage = str(exception_item.value or "integer_conv")
                raise NodeExecutionError(
                    f"node {task.node_id}: injected exception at {stage}",
                    node_id=task.node_id,
                    details={"injection_id": exception_item.id, "stage": stage},
                )
            result = compute_node(
                task.node,
                task.source,
                task.weight_pack,
                task.params,
                quantization_enabled=self.quantization_enabled,
                rounding_mode=self.schedule.rounding_mode,
                dequant_decimals=self.schedule.dequant_decimals,
                debug_node_delay_ms=self.schedule.debug_node_delay_ms,
            )
            return TaskOutcome(
                task=task,
                success=True,
                result=result,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                injection_hits=hits,
                executed_macs=result.macs_executed,
            )
        except QConvError as exc:
            failure = exc.to_failure()
            if isinstance(exc, NodeExecutionError):
                failure.stage = str(failure.details.get("stage", failure.stage))
            executed = 0
            if failure.code == "E_ACC_OVERFLOW":
                try:
                    executed = int(failure.details["position"]["mac_index"]) + 1
                except (KeyError, TypeError, ValueError):
                    executed = 0
            return TaskOutcome(
                task=task,
                success=False,
                failure=failure,
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                injection_hits=hits,
                executed_macs=executed,
            )
        except Exception as exc:
            return TaskOutcome(
                task=task,
                success=False,
                failure=Failure(
                    code="E_EXECUTION_FAILED",
                    message=f"node {task.node_id}: unhandled execution exception: {exc}",
                    stage="integer_conv",
                    node_id=task.node_id,
                    exception_type=type(exc).__name__,
                ),
                elapsed_ms=(time.perf_counter() - started) * 1000.0,
                injection_hits=hits,
                executed_macs=0,
            )

    def _snapshot_payload(
        self,
        stage: str,
        completed: Iterable[str],
        artifact_versions: dict[str, dict[str, Any]],
        *,
        interrupted: bool,
    ) -> dict[str, Any]:
        graph = self._require_graph()
        completed_sorted = graph.sorted_nodes(completed)
        partial = {
            node_id: artifact_versions[node_id]["output_digest"]
            for node_id in completed_sorted
            if node_id in artifact_versions
        }
        return {
            "schema_version": 1,
            "stage": stage,
            "global_graph_version": graph.graph_version,
            "graph_fingerprint": graph.fingerprint(),
            "config_hash": self.config_hash,
            "completed_nodes": completed_sorted,
            "partial_tensor_digests": partial,
            "artifact_versions": {
                node_id: artifact_versions[node_id]
                for node_id in completed_sorted
                if node_id in artifact_versions
            },
            "quantization_enabled": self.quantization_enabled,
            "interrupted": interrupted,
        }

    def _base_report(self, *, status: str, failures: list[Failure], **extra: Any) -> dict[str, Any]:
        graph = self.graph
        report: dict[str, Any] = {
            "schema_version": 1,
            "status": status,
            "implementation_version": IMPLEMENTATION_VERSION,
            "config_hash": self.config_hash,
            "graph_version": graph.graph_version if graph is not None else None,
            "graph_fingerprint": graph.fingerprint() if graph is not None else None,
            "graph_source_hash": self.graph_source_hash,
            "topology": {
                "order": list(graph.topology) if graph is not None else [],
                "levels": dict(graph.levels) if graph is not None else {},
                "stable_sort_keys": {
                    node_id: graph.sort_key(node_id) for node_id in graph.topology
                }
                if graph is not None
                else {},
            },
            "schedule": self.schedule.to_dict(),
            "quantization_enabled": self.quantization_enabled,
            "rounding_mode": self.schedule.rounding_mode,
            "invalidated_nodes": graph.sorted_nodes(self.pending_invalidations)
            if graph is not None
            else [],
            "recomputed_nodes": [],
            "reused_nodes": [],
            "restored_from_snapshot": [],
            "budget": {
                "global_limit": self.schedule.global_mac_budget,
                "per_node_quota": self.schedule.per_node_mac_quota,
                "remaining": self.schedule.global_mac_budget,
                "ledger": [],
            },
            "nodes": [],
            "failures": [failure.to_dict() for failure in failures],
            "injection_hits": [],
            "cache": {},
            "snapshot": {
                "version": self.session.snapshot_version,
                "stage": None,
                "path": str(self.session.latest_path),
            },
            "first_frame_response": {
                "latency_ms": None,
                "budget_ms": self.schedule.first_frame_response_budget_ms,
                "met": None,
            },
            "interrupted": False,
            "graceful_stop": False,
            "working_graph_loaded": self.working_graph_loaded,
        }
        report.update(extra)
        return report

    def _write_report(self, report: dict[str, Any], report_path: str | Path | None) -> None:
        if report_path is not None:
            atomic_write_json(Path(report_path), report)
        self.last_report = report

    # ------------------------------------------------------------------
    # Main run
    # ------------------------------------------------------------------
    def run(
        self,
        report_path: str | Path | None = None,
        *,
        stop_event: threading.Event | None = None,
    ) -> dict[str, Any]:
        stop = stop_event or threading.Event()
        graph = self.graph
        if graph is None or self.fatal:
            failures = list(self.load_failures)
            if not failures:
                failures.append(
                    Failure(
                        code=STRUCTURAL_CODE,
                        message="graph is unavailable",
                        stage="graph_load",
                    )
                )
            report = self._base_report(status="failed", failures=failures)
            report["cache"] = {
                "budget_bytes": self.schedule.cache_residency_bytes,
                "used_bytes": 0,
                "artifact_count": 0,
                "hits": [],
                "misses": [],
                "evictions": [],
            }
            self._write_report(report, report_path)
            return report

        # A replaced subgraph may leave stale in-memory products for node IDs
        # that no longer exist.  They must never leak into the current report.
        self.last_results = {
            node_id: result for node_id, result in self.last_results.items() if node_id in graph.nodes
        }

        # Snapshot configuration compatibility is checked before any work.
        if self.loaded_snapshot is not None:
            saved_hash = self.loaded_snapshot.get("config_hash")
            if saved_hash != self.config_hash:
                failure = ConfigMismatchError(
                    "snapshot config hash does not match the current scheduling/injection config",
                    details={"snapshot_config_hash": saved_hash, "current_config_hash": self.config_hash},
                ).to_failure()
                report = self._base_report(status="failed", failures=[failure])
                report["snapshot"]["version"] = int(self.loaded_snapshot.get("snapshot_version", 0))
                report["snapshot"]["stage"] = self.loaded_snapshot.get("stage")
                self._write_report(report, report_path)
                return report

        effective_cache_budget = (
            0 if self.injection.has_cache_budget_zero() else self.schedule.cache_residency_bytes
        )
        cache = ArtifactCache(self.session.cache_dir, effective_cache_budget)
        injection_hits: list[dict[str, Any]] = []
        if self.injection.has_cache_budget_zero():
            removed = cache.clear()
            for item in self.injection.items:
                if item.type == "cache_budget_zero":
                    injection_hits.append(
                        {
                            "id": item.id,
                            "type": item.type,
                            "node_id": None,
                            "stage": "config_load",
                            "effect": f"cache_residency_bytes=0; removed={len(removed)}",
                        }
                    )

        # Validate injection node references before scheduling.
        injection_failures: list[Failure] = []
        for item in self.injection.items:
            if item.node is not None and item.node not in graph.nodes:
                injection_failures.append(
                    Failure(
                        code="E_INJECTION_INVALID",
                        message=f"injection {item.id} references unknown node {item.node}",
                        stage="config_load",
                        node_id=item.node,
                    )
                )
        if injection_failures:
            report = self._base_report(status="failed", failures=injection_failures)
            report["injection_hits"] = injection_hits
            report["cache"] = cache.report()
            self._write_report(report, report_path)
            return report

        run_started = time.perf_counter()
        graph_fingerprint = graph.fingerprint()
        initial_completed: set[str] = set()
        snapshot_restorable = False
        if self.loaded_snapshot is not None:
            snapshot_restorable = self.loaded_snapshot.get("graph_fingerprint") == graph_fingerprint
            if snapshot_restorable:
                initial_completed = set(self.loaded_snapshot.get("completed_nodes", []))

        completed: set[str] = set(initial_completed)
        artifact_versions: dict[str, dict[str, Any]] = {}
        if snapshot_restorable:
            for node_id, version in self.loaded_snapshot.get("artifact_versions", {}).items():
                if isinstance(version, dict):
                    artifact_versions[node_id] = dict(version)
        completed_since_snapshot = 0

        # Phase 1: graph load snapshot.
        graph_load_payload = self._snapshot_payload(
            "graph_load", completed, artifact_versions, interrupted=False
        )
        self.session.commit(graph_load_payload)
        integer_phase_payload = self._snapshot_payload(
            "integer_conv", completed, artifact_versions, interrupted=False
        )
        self.session.commit(integer_phase_payload)

        failures: list[Failure] = list(self.load_failures)
        nodes_report: dict[str, dict[str, Any]] = {}
        ledger: dict[str, dict[str, Any]] = {}
        for node_id in graph.topology:
            node = graph.nodes[node_id]
            nodes_report[node_id] = {
                "node_id": node_id,
                "sort_key": graph.sort_key(node_id),
                "status": "pending",
                "stage": "scheduler",
                "cache_key": None,
                "cache_hit": False,
                "invalidated": node_id in self.pending_invalidations,
                "reused": False,
                "tensor": node.output_tensor,
                "output_shape": list(graph.output_shapes.get(node.output_tensor, [])),
                "input_digest": None,
                "integer_digest": None,
                "output_digest": None,
                "rounding_mode": self.schedule.rounding_mode,
                "dequant_decimals": self.schedule.dequant_decimals,
                "macs_expected": self._graph_scalar_required_macs(node),
                "macs_executed": 0,
                "recompute_elapsed_ms": 0.0,
                "error_summary": {},
                "injection_ids": [],
                "output_preview": [],
                "failure_codes": [],
            }
            ledger[node_id] = {
                "node_id": node_id,
                "sort_key": graph.sort_key(node_id),
                "status": "pending",
                "required_macs": self._graph_scalar_required_macs(node),
                "requested_macs": 0,
                "reserved_macs": 0,
                "executed_macs": 0,
                "remaining_before": None,
                "remaining_after": None,
                "remaining_after_reconcile": None,
                "remaining_after_reservation": None,
                "refund_macs": 0,
                "reason": None,
            }

        invalid_param_nodes = set(self.node_parameter_failures)
        unavailable: set[str] = set(invalid_param_nodes)
        remaining_budget = self.schedule.global_mac_budget
        first_frame_ms: float | None = None
        recomputed: list[str] = []
        reused: list[str] = []
        restored: list[str] = []
        successful: set[str] = set()
        interrupted = False
        completed_since_snapshot = 0

        def first_frame_if_needed() -> None:
            nonlocal first_frame_ms
            if first_frame_ms is None:
                first_frame_ms = (time.perf_counter() - run_started) * 1000.0

        def mark_success(node_id: str, result: ConvResult, *, was_reused: bool, restored_node: bool) -> None:
            nonlocal completed_since_snapshot
            successful.add(node_id)
            completed.add(node_id)
            record = nodes_report[node_id]
            record.update(
                {
                    "status": "reused" if was_reused else "computed",
                    "stage": "dequant",
                    "cache_hit": was_reused,
                    "reused": was_reused,
                    "integer_digest": result.integer_digest,
                    "output_digest": result.output_digest,
                    "output_shape": list(result.output.shape),
                    "rounding_mode": result.metadata.get(
                        "rounding_mode", self.schedule.rounding_mode
                    ),
                    "dequant_decimals": result.metadata.get(
                        "dequant_decimals", self.schedule.dequant_decimals
                    ),
                    "macs_expected": result.macs_expected,
                    "macs_executed": 0 if was_reused else result.macs_executed,
                    "recompute_elapsed_ms": 0.0 if was_reused else result.elapsed_ms,
                    "error_summary": result.error_summary,
                    "output_preview": [
                        float(value) for value in np.asarray(result.output, dtype=np.float64).reshape(-1)[:8]
                    ],
                }
            )
            artifact_versions[node_id] = {
                "cache_key": record.get("cache_key"),
                "output_digest": result.output_digest,
                "integer_digest": result.integer_digest,
                "artifact_version": f"{IMPLEMENTATION_VERSION}:g{graph.graph_version}",
            }
            if was_reused:
                reused.append(node_id)
                if restored_node:
                    restored.append(node_id)
            else:
                recomputed.append(node_id)
            first_frame_if_needed()
            completed_since_snapshot += 1
            if completed_since_snapshot >= self.schedule.snapshot_commit_interval:
                payload = self._snapshot_payload(
                    "dequant", completed, artifact_versions, interrupted=False
                )
                self.session.commit(payload)
                completed_since_snapshot = 0

        def mark_failure(node_id: str, failure: Failure) -> None:
            unavailable.add(node_id)
            nodes_report[node_id]["status"] = "failed"
            nodes_report[node_id]["stage"] = failure.stage
            nodes_report[node_id]["failure_codes"] = [failure.code]
            failures.append(failure)

        levels = sorted(set(graph.levels.values()))
        for level in levels:
            if stop.is_set():
                interrupted = True
                break
            level_nodes = [node_id for node_id in graph.topology if graph.levels[node_id] == level]
            tasks: list[TaskSpec] = []

            for node_id in level_nodes:
                node = graph.nodes[node_id]
                record = nodes_report[node_id]
                if node_id in invalid_param_nodes:
                    load_errors = self.node_parameter_failures.get(node_id, [])
                    record["status"] = "failed"
                    record["stage"] = "parameter_construction"
                    record["failure_codes"] = [failure.code for failure in load_errors]
                    unavailable.add(node_id)
                    continue
                predecessors = graph.predecessors(node_id)
                blocked = [pred for pred in predecessors if pred in unavailable]
                if blocked:
                    failure = DependencyFailedError(
                        f"node {node_id} cannot run because upstream nodes failed: {sorted(blocked)}",
                        node_id=node_id,
                        details={"blocked_by": sorted(blocked)},
                    ).to_failure()
                    mark_failure(node_id, failure)
                    ledger[node_id].update(
                        {
                            "status": "dependency_failed",
                            "remaining_before": remaining_budget,
                            "remaining_after": remaining_budget,
                            "remaining_after_reconcile": remaining_budget,
                            "reason": "upstream unavailable",
                        }
                    )
                    continue
                try:
                    source = self._make_source(node)
                except QConvError as exc:
                    mark_failure(node_id, exc.to_failure())
                    continue
                items = tuple(self.injection.items_for_node(node_id))
                params, injection_hits_for_node = self._params_and_injections(node, items)
                weight_pack = self.pack_repository.weight_for(node) if self.pack_repository else None
                if weight_pack is None:
                    mark_failure(
                        node_id,
                        PackInvalidError("weight pack repository is unavailable", node_id=node_id).to_failure(),
                    )
                    continue
                cache_key = self._cache_key(node, source, params, weight_pack, items)
                record["cache_key"] = cache_key
                record["input_digest"] = source.source_digest
                record["injection_ids"] = [item.id for item in items]
                injection_hits.extend(injection_hits_for_node)
                required = expected_macs(node, source.integer.shape)
                ledger[node_id]["required_macs"] = required

                forced_miss = node_id in self.pending_invalidations or bool(items)
                artifact = None
                if not forced_miss:
                    artifact = cache.load(cache_key)
                else:
                    cache.misses.append(cache_key)
                if artifact is not None and artifact.output.shape == tuple(
                    graph.output_shapes[node.output_tensor]
                ):
                    result = ConvResult(
                        integer=artifact.integer,
                        output=artifact.output,
                        integer_digest=artifact.integer_digest,
                        output_digest=artifact.output_digest,
                        macs_executed=artifact.macs_executed,
                        macs_expected=artifact.macs_expected,
                        activation_clamped=int(artifact.metadata.get("activation_requant_clamped", 0)),
                        weight_clamped=int(artifact.metadata.get("weight_requant_clamped", 0)),
                        error_summary=dict(artifact.error_summary),
                        metadata=artifact.metadata,
                        elapsed_ms=0.0,
                    )
                    self.last_results[node_id] = result
                    ledger[node_id].update(
                        {
                            "status": "cache_reused",
                            "remaining_before": remaining_budget,
                            "remaining_after": remaining_budget,
                            "remaining_after_reconcile": remaining_budget,
                            "reason": "content cache hit",
                        }
                    )
                    mark_success(
                        node_id,
                        result,
                        was_reused=True,
                        restored_node=snapshot_restorable and node_id in initial_completed,
                    )
                    continue

                ledger[node_id]["requested_macs"] = required
                ledger[node_id]["remaining_before"] = remaining_budget
                if required > self.schedule.per_node_mac_quota:
                    failure = BudgetExhaustedError(
                        f"node {node_id} requires {required} MACs, exceeding per-node quota "
                        f"{self.schedule.per_node_mac_quota}",
                        node_id=node_id,
                        details={
                            "required_macs": required,
                            "per_node_mac_quota": self.schedule.per_node_mac_quota,
                        },
                    ).to_failure()
                    mark_failure(node_id, failure)
                    ledger[node_id].update(
                        {
                            "status": "budget_exhausted",
                            "remaining_after": remaining_budget,
                            "remaining_after_reconcile": remaining_budget,
                            "reason": "per_node_mac_quota",
                        }
                    )
                    continue
                if required > remaining_budget:
                    failure = BudgetExhaustedError(
                        f"node {node_id} requires {required} MACs, exceeding remaining global budget "
                        f"{remaining_budget}",
                        node_id=node_id,
                        details={
                            "required_macs": required,
                            "remaining_global_budget": remaining_budget,
                        },
                    ).to_failure()
                    mark_failure(node_id, failure)
                    ledger[node_id].update(
                        {
                            "status": "budget_exhausted",
                            "remaining_after": remaining_budget,
                            "remaining_after_reconcile": remaining_budget,
                            "reason": "global_mac_budget",
                        }
                    )
                    continue
                remaining_budget -= required
                ledger[node_id].update(
                    {
                        "status": "admitted",
                        "reserved_macs": required,
                        "remaining_after": remaining_budget,
                        "remaining_after_reconcile": remaining_budget,
                        "remaining_after_reservation": remaining_budget,
                        "reason": "admitted",
                    }
                )
                tasks.append(
                    TaskSpec(
                        node_id=node_id,
                        sort_key=graph.sort_key(node_id),
                        node=node,
                        source=source,
                        weight_pack=weight_pack,
                        params=params,
                        cache_key=cache_key,
                        requested_macs=required,
                        injection_items=items,
                    )
                )

            if not tasks:
                continue
            outcomes: dict[str, TaskOutcome] = {}
            executor = ThreadPoolExecutor(
                max_workers=self.schedule.max_concurrency,
                thread_name_prefix="qconv",
            )
            try:
                futures = {executor.submit(self._execute_task, task): task for task in tasks}
                for future in as_completed(futures):
                    outcome = future.result()
                    outcomes[outcome.task.node_id] = outcome
                    if outcome.success:
                        first_frame_if_needed()
            finally:
                executor.shutdown(wait=True, cancel_futures=False)

            for task in tasks:
                node_id = task.node_id
                outcome = outcomes[node_id]
                ledger[node_id]["executed_macs"] = outcome.executed_macs
                if outcome.success and outcome.result is not None:
                    self.last_results[node_id] = outcome.result
                    ledger[node_id].update(
                        {
                            "status": "executed",
                            "reason": "executed",
                        }
                    )
                    artifact = CacheArtifact(
                        cache_key=task.cache_key,
                        node_id=node_id,
                        integer=outcome.result.integer,
                        output=outcome.result.output,
                        integer_digest=outcome.result.integer_digest,
                        output_digest=outcome.result.output_digest,
                        metadata=outcome.result.metadata,
                        error_summary=outcome.result.error_summary,
                        macs_executed=outcome.result.macs_executed,
                        macs_expected=outcome.result.macs_expected,
                    )
                    cache.store(artifact)
                    mark_success(
                        node_id,
                        outcome.result,
                        was_reused=False,
                        restored_node=False,
                    )
                else:
                    refund = task.requested_macs - outcome.executed_macs
                    if refund > 0:
                        remaining_budget += refund
                    ledger[node_id]["refund_macs"] = refund
                    if ledger[node_id]["remaining_after"] is not None:
                        ledger[node_id]["remaining_after_reconcile"] = (
                            ledger[node_id]["remaining_after"] + refund
                        )
                    failure = outcome.failure or Failure(
                        code="E_EXECUTION_FAILED",
                        message=f"node {node_id} produced no result",
                        stage="dequant",
                        node_id=node_id,
                    )
                    mark_failure(node_id, failure)
                    nodes_report[node_id]["recompute_elapsed_ms"] = outcome.elapsed_ms
                    ledger[node_id].update(
                        {
                            "status": "failed",
                            "reason": failure.code,
                        }
                    )
            if stop.is_set():
                interrupted = True
                break

        if interrupted:
            for node_id in graph.topology:
                if nodes_report[node_id]["status"] == "pending":
                    nodes_report[node_id]["status"] = "skipped_graceful_stop"
                    nodes_report[node_id]["stage"] = "scheduler"

        # Reconcile ledger statuses for pending entries.
        for node_id in graph.topology:
            if ledger[node_id]["status"] == "admitted":
                ledger[node_id]["status"] = "executed"

        # Stage snapshots: dequant and aggregate.  The dequant snapshot is
        # committed even for an interrupted run, allowing exact resumption.
        dequant_payload = self._snapshot_payload(
            "dequant", completed, artifact_versions, interrupted=interrupted
        )
        self.session.commit(dequant_payload)
        aggregate_payload = self._snapshot_payload(
            "aggregate", completed, artifact_versions, interrupted=interrupted
        )
        final_snapshot = self.session.commit(aggregate_payload)

        failures.sort(
            key=lambda failure: (
                graph.sort_key(failure.node_id) if failure.node_id in graph.nodes else "99999999",
                failure.code,
                failure.message,
            )
        )
        recomputed = graph.sorted_nodes(recomputed)
        reused = graph.sorted_nodes(reused)
        restored = graph.sorted_nodes(restored)
        report_nodes = [nodes_report[node_id] for node_id in graph.topology]
        for record in report_nodes:
            if record["status"] in {"failed", "budget_exhausted", "dependency_failed"}:
                record["status"] = (
                    "budget_exhausted" if "E_BUDGET_EXHAUSTED" in record["failure_codes"] else record["status"]
                )
        ledger_rows: list[dict[str, Any]] = []
        for sequence, node_id in enumerate(graph.topology, start=1):
            row = dict(ledger[node_id])
            row["sequence"] = sequence
            if row["status"] == "pending":
                row["status"] = "not_run"
            ledger_rows.append(row)

        first_frame_met = (
            None
            if first_frame_ms is None
            else first_frame_ms <= self.schedule.first_frame_response_budget_ms
        )
        status = "interrupted" if interrupted else ("success" if not failures else "partial")
        report = self._base_report(status=status, failures=failures)
        report.update(
            {
                "recomputed_nodes": recomputed,
                "reused_nodes": reused,
                "restored_from_snapshot": restored,
                "budget": {
                    "global_limit": self.schedule.global_mac_budget,
                    "per_node_quota": self.schedule.per_node_mac_quota,
                    "remaining": remaining_budget,
                    "reserved_total": sum(
                        int(row["reserved_macs"]) for row in ledger_rows
                    ),
                    "executed_total": sum(int(row["executed_macs"]) for row in ledger_rows),
                    "ledger": ledger_rows,
                },
                "nodes": report_nodes,
                "injection_hits": injection_hits,
                "cache": cache.report(),
                "snapshot": {
                    "version": int(final_snapshot["snapshot_version"]),
                    "stage": final_snapshot["stage"],
                    "path": str(self.session.latest_path),
                },
                "first_frame_response": {
                    "latency_ms": first_frame_ms,
                    "budget_ms": self.schedule.first_frame_response_budget_ms,
                    "met": first_frame_met,
                },
                "interrupted": interrupted,
                "graceful_stop": interrupted,
                "working_graph_loaded": self.working_graph_loaded,
            }
        )
        # Successful invalidations are reconciled; failed ones remain pending.
        self.pending_invalidations = {
            node_id for node_id in self.pending_invalidations if node_id not in successful
        }
        self._write_report(report, report_path)
        return report
