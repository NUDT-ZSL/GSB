"""Topological, cache-aware, concurrent execution and edit invalidation."""

from __future__ import annotations

import os
import signal
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import numpy as np

from .config import RuntimeConfig, parse_config
from .edits import apply_edit_operation
from .errors import (
    BusinessError,
    E_CONFIG_MISMATCH,
    E_DEPENDENCY_FAILED,
    E_GRAPH_INVALID,
    E_INPUT_INVALID,
    E_NONFINITE_INPUT,
    E_PARAM_OUT_OF_RANGE,
    E_SHAPE_MISMATCH,
    failure_from_exception,
)
from .graph import Graph, ValidationResult, validate_graph
from .injections import InjectionItem, injection_hit, input_injections, node_injections
from .ops import cache_key, execute_operator
from .runtime import StopController
from .state import StateStore
from .util import (
    deep_copy_json,
    sha256_json,
    sorted_failures,
    stable_sort_key,
    tensor_fingerprint,
    tensor_summary,
)


BLOCKED_STATUSES = {"failed", "skipped_dependency", "skipped_disabled", "pending_interrupt"}
SUCCESS_STATUSES = {"reused", "recalculated"}


@dataclass
class RunOutcome:
    report: dict[str, Any]
    snapshot: dict[str, Any]
    graph: Graph | None
    stop_requested: bool


@dataclass
class EditOutcome:
    committed: bool
    snapshot: dict[str, Any]
    graph: Graph | None
    failures: list[dict[str, Any]]
    edit_info: dict[str, Any]
    invalidated_nodes: list[str]
    reused_nodes: list[str]
    removed_nodes: list[str]
    invalidation_chains: dict[str, list[str]]


def product_version(graph_version: int, cache_key_value: str) -> str:
    return f"pv-g{int(graph_version):04d}-{cache_key_value[:16]}"


def _failure(
    code: str,
    message: str,
    *,
    node_id: str | None = None,
    stage: str,
    details: dict[str, Any] | None = None,
    injection_id: str | None = None,
) -> dict[str, Any]:
    return {
        "code": code,
        "message": message,
        "node_id": node_id,
        "stage": stage,
        "exception_type": "BusinessError",
        **({"details": details} if details else {}),
        **({"injection_id": injection_id} if injection_id else {}),
    }


def _dedupe_injection_hits(hits: list[dict[str, Any]], new_hit: dict[str, Any]) -> None:
    signature = (
        new_hit.get("id"),
        new_hit.get("type"),
        new_hit.get("node_id"),
        new_hit.get("effect"),
        new_hit.get("stage"),
    )
    for existing in hits:
        if signature == (
            existing.get("id"),
            existing.get("type"),
            existing.get("node_id"),
            existing.get("effect"),
            existing.get("stage"),
        ):
            return
    hits.append(new_hit)


class Engine:
    def __init__(
        self,
        store: StateStore,
        config_raw: dict[str, Any],
        injections: list[InjectionItem] | None = None,
    ) -> None:
        self.store = store
        self.config_raw = config_raw
        self.config: RuntimeConfig = parse_config(config_raw)
        self.injections = list(injections or [])

    def _verify_snapshot_identity(self, snapshot: dict[str, Any], graph_raw: dict[str, Any]) -> None:
        if sha256_json(self.config_raw) != snapshot.get("config_hash"):
            raise BusinessError(
                E_CONFIG_MISMATCH,
                "configuration hash does not match the snapshot",
                stage="run",
                details={
                    "snapshot_config_hash": snapshot.get("config_hash"),
                    "current_config_hash": sha256_json(self.config_raw),
                },
            )
        if sha256_json(graph_raw) != snapshot.get("graph_fingerprint"):
            raise BusinessError(
                E_CONFIG_MISMATCH,
                "graph content fingerprint does not match the snapshot",
                stage="run",
                details={
                    "snapshot_graph_fingerprint": snapshot.get("graph_fingerprint"),
                    "current_graph_fingerprint": sha256_json(graph_raw),
                },
            )

    def _validate_graph_or_fail(self, raw_graph: Any) -> ValidationResult:
        validation = validate_graph(raw_graph)
        return validation

    def _apply_input_injections(
        self,
        snapshot: dict[str, Any],
        input_array: np.ndarray,
        tensor_id: str,
    ) -> tuple[np.ndarray, list[dict[str, Any]]]:
        items = input_injections(self.injections, tensor_id)
        if not items:
            return input_array, []
        array = np.array(input_array, dtype=np.float64, copy=True)
        hits: list[dict[str, Any]] = []
        shape = array.shape
        for item in sorted(items, key=lambda value: value.id):
            position = item.raw["index"]
            for axis, index in enumerate(position):
                if not 0 <= int(index) < shape[axis]:
                    raise BusinessError(
                        E_PARAM_OUT_OF_RANGE,
                        "nonfinite_input injection index is outside the input tensor",
                        stage="injection",
                        injection_id=item.id,
                        details={"index": position, "shape": list(shape)},
                    )
            raw_value = item.raw.get("value", "nan")
            if isinstance(raw_value, str):
                lowered = raw_value.lower()
                if lowered == "nan":
                    value = float("nan")
                elif lowered in {"inf", "+inf"}:
                    value = float("inf")
                elif lowered in {"-inf"}:
                    value = float("-inf")
                else:
                    raise BusinessError(
                        E_PARAM_OUT_OF_RANGE,
                        "nonfinite_input injection value must be nan, inf, or -inf",
                        stage="injection",
                        injection_id=item.id,
                    )
            else:
                value = float(raw_value)
            array[tuple(int(index) for index in position)] = np.float64(value)
            _dedupe_injection_hits(
                hits,
                injection_hit(
                    item,
                    stage="input_generation",
                    effect="applied_nonfinite_value",
                    index=[int(index) for index in position],
                    value=str(value),
                ),
            )
        array = np.ascontiguousarray(array, dtype=np.float64)
        descriptor_raw = self.store.load_input_descriptor()
        updates = self.store.replace_input_array(array, descriptor_raw)
        snapshot.update(updates)
        return array, hits

    def run(
        self,
        snapshot: dict[str, Any],
        *,
        stop_controller: StopController | None = None,
        full_recompute: bool = False,
    ) -> RunOutcome:
        controller = stop_controller or StopController()
        snapshot = deep_copy_json(snapshot)
        graph_raw = snapshot.get("graph")
        self._verify_snapshot_identity(snapshot, graph_raw)
        validation = self._validate_graph_or_fail(graph_raw)
        if validation.graph is None:
            return self._invalid_graph_outcome(snapshot, validation, controller)
        graph = validation.graph
        if validation.failures:
            return self._invalid_graph_outcome(snapshot, validation, controller)

        graph_version = int(snapshot.get("graph_version", 0))
        snapshot["topology"] = graph.topology_records()
        snapshot["topology_index"] = dict(graph.topo_index)
        snapshot["failures"] = []
        snapshot["execution_trace"] = []
        snapshot.setdefault("injections_hit", [])
        snapshot["run_epoch"] = int(snapshot.get("run_epoch", 0)) + 1

        input_tensor_id = next(iter(graph.input_tensors))
        if len(graph.input_tensors) != 1:
            raise BusinessError(
                E_INPUT_INVALID,
                "this application accepts exactly one external input tensor",
                stage="input",
                details={"input_tensors": sorted(graph.input_tensors)},
            )
        expected_input_fingerprint = snapshot.get("input_fingerprint")
        input_array = self.store.load_input_array(expected_input_fingerprint)
        input_array, input_hits = self._apply_input_injections(snapshot, input_array, input_tensor_id)
        for hit in input_hits:
            _dedupe_injection_hits(snapshot["injections_hit"], hit)
        input_shape = graph.input_tensors[input_tensor_id]["shape"]
        if list(input_array.shape) != input_shape:
            raise BusinessError(
                E_SHAPE_MISMATCH,
                "input tensor shape does not match graph input metadata",
                stage="input",
                details={"input_shape": list(input_array.shape), "graph_shape": input_shape},
            )
        current_input_fingerprint = tensor_fingerprint(input_array)
        snapshot["input_fingerprint"] = current_input_fingerprint
        snapshot["input_summary"] = tensor_summary(input_array)
        if current_input_fingerprint != expected_input_fingerprint and not full_recompute:
            full_recompute = True

        node_products: dict[str, dict[str, Any]] = {}
        product_arrays: dict[str, np.ndarray] = {}
        invalid_record_roots: set[str] = set()
        old_records = snapshot.get("node_products", {}) if isinstance(snapshot.get("node_products"), dict) else {}
        if not full_recompute:
            for node_id, record in old_records.items():
                if node_id not in graph.nodes:
                    continue
                expected_prefix = f"pv-g{graph_version:04d}-"
                if not isinstance(record, dict) or not str(record.get("product_version", "")).startswith(expected_prefix):
                    invalid_record_roots.add(node_id)
                    continue
                array = self.store.load_product(record)
                if array is None:
                    invalid_record_roots.add(node_id)
                    continue
                node_products[node_id] = deep_copy_json(record)
                product_arrays[node_id] = array
        else:
            node_products = {}
            product_arrays = {}

        recalc_nodes: set[str] = set()
        for node_id in graph.nodes:
            if node_id not in node_products:
                recalc_nodes.add(node_id)
        recalc_nodes.update(invalid_record_roots)
        if invalid_record_roots:
            recalc_nodes.update(graph.descendants(invalid_record_roots))

        injection_roots: set[str] = set()
        for node_id in graph.nodes:
            grouped = node_injections(self.injections, node_id)
            if any(key in grouped for key in ("shape_mismatch", "padding_out_of_range", "invalid_scale")):
                injection_roots.add(node_id)
        if injection_roots:
            recalc_nodes.update(graph.descendants(injection_roots))
            for node_id in graph.descendants(injection_roots):
                node_products.pop(node_id, None)
                product_arrays.pop(node_id, None)

        for node_id in list(node_products):
            if not graph.nodes[node_id].enabled:
                node_products.pop(node_id, None)
                product_arrays.pop(node_id, None)
                recalc_nodes.update(graph.descendants({node_id}, include_roots=False))

        status_map: dict[str, str] = {}
        failures: list[dict[str, Any]] = []
        for node_id in graph.topo_order:
            node = graph.nodes[node_id]
            if not node.enabled:
                status_map[node_id] = "skipped_disabled"
            elif node_id in node_products:
                status_map[node_id] = "reused"
            else:
                status_map[node_id] = "pending"

        reused_now = [node_id for node_id in graph.topo_order if status_map[node_id] == "reused"]
        requested_invalidated = sorted(
            set(snapshot.get("invalidated_nodes", [])) | injection_roots | invalid_record_roots,
            key=lambda item: graph.topo_index.get(item, 10**9),
        )
        snapshot["invalidated_nodes"] = requested_invalidated
        snapshot["recalculated_nodes"] = [node_id for node_id in graph.topo_order if node_id in recalc_nodes]
        snapshot["reused_nodes"] = reused_now
        snapshot["node_products"] = node_products
        snapshot["completed_nodes"] = reused_now
        snapshot["node_status"] = status_map
        snapshot["status"] = "scheduled"
        snapshot["stage"] = "topology_scheduled"
        snapshot["failures"] = []
        self._normalize_snapshot(snapshot, graph)
        self.store.save(snapshot)

        pending_execution = any(status == "pending" for status in status_map.values())
        pooled = ThreadPoolExecutor(max_workers=self.config.max_workers) if pending_execution else None
        execution_trace: list[str] = []
        interrupt_triggered: set[str] = set()
        hard_kill_triggered: set[str] = set()
        try:
            while True:
                if controller.requested:
                    break
                if pending_execution and pooled is None:
                    pooled = ThreadPoolExecutor(max_workers=self.config.max_workers)
                self._resolve_blocked_dependencies(graph, status_map, failures)
                pending = [node_id for node_id in graph.topo_order if status_map[node_id] == "pending"]
                if not pending:
                    break
                ready = [
                    node_id
                    for node_id in pending
                    if self._inputs_available(graph, node_id, status_map)
                ]
                ready.sort(key=lambda item: (graph.topo_index[item], item))
                if not ready:
                    break
                if controller.requested:
                    break

                for node_id in ready:
                    for item in node_injections(self.injections, node_id).get("hard_kill", []):
                        phase = str(item.raw.get("phase", "during_compute"))
                        key = f"{item.id}:{phase}:{node_id}"
                        if phase != "after_snapshot" and key not in hard_kill_triggered:
                            hard_kill_triggered.add(key)
                            _dedupe_injection_hits(
                                snapshot["injections_hit"],
                                injection_hit(item, stage="node_execution", effect="hard_kill_armed", phase=phase),
                            )
                snapshot["status"] = "executing"
                snapshot["stage"] = "node_execution"
                self._normalize_snapshot(snapshot, graph)
                self.store.save(snapshot)

                futures = {}
                for node_id in ready:
                    source_arrays = self._collect_input_arrays(
                        graph, node_id, input_tensor_id, input_array, product_arrays
                    )
                    stable_key = stable_sort_key(graph.topo_index[node_id], node_id)
                    futures[
                        pooled.submit(
                            self._execute_node,
                            graph,
                            graph_version,
                            node_id,
                            source_arrays,
                            stable_key,
                        )
                    ] = node_id

                wave_nodes: list[str] = []
                for future in as_completed(futures):
                    node_id = futures[future]
                    wave_nodes.append(node_id)
                    try:
                        result = future.result()
                    except BaseException as exc:
                        result = {
                            "node_id": node_id,
                            "status": "failed",
                            "record": None,
                            "array": None,
                            "failure": failure_from_exception(exc, node_id=node_id, stage="compute"),
                            "injections": [],
                            "stable_sort_key": stable_sort_key(graph.topo_index[node_id], node_id),
                        }
                    execution_trace.append(node_id)
                    status_map[node_id] = str(result["status"])
                    for hit in result.get("injections", []):
                        _dedupe_injection_hits(snapshot["injections_hit"], hit)
                    record = result.get("record")
                    if record is not None:
                        node_products[node_id] = record
                        if result.get("array") is not None:
                            product_arrays[node_id] = result["array"]
                    failure = result.get("failure")
                    if failure:
                        failures.append(failure)
                    snapshot["node_products"] = node_products
                    snapshot["node_status"] = status_map
                    snapshot["completed_nodes"] = [
                        item for item in graph.topo_order if status_map.get(item) in SUCCESS_STATUSES
                    ]
                    snapshot["failures"] = sorted_failures(failures, graph.topo_index)
                    snapshot["execution_trace"] = sorted(
                        set(execution_trace), key=lambda item: (graph.topo_index[item], item)
                    )
                    snapshot["status"] = "executing"
                    snapshot["stage"] = "node_execution"
                    self._normalize_snapshot(snapshot, graph)
                    self.store.save(snapshot)

                wave_nodes = sorted(wave_nodes, key=lambda item: (graph.topo_index[item], item))
                requested_after_wave = False
                for node_id in wave_nodes:
                    for item in node_injections(self.injections, node_id).get("interrupt", []):
                        if item.id in interrupt_triggered:
                            continue
                        interrupt_triggered.add(item.id)
                        signal_name = str(item.raw.get("signal", "SIGTERM"))
                        _dedupe_injection_hits(
                            snapshot["injections_hit"],
                            injection_hit(
                                item,
                                stage="node_snapshot_committed",
                                effect="graceful_stop_requested",
                                signal=signal_name,
                            ),
                        )
                        self._normalize_snapshot(snapshot, graph)
                        self.store.save(snapshot)
                        if controller.installed:
                            os.kill(os.getpid(), getattr(signal, signal_name))
                        else:
                            controller.request_stop(signal_name)
                        requested_after_wave = True
                    for item in node_injections(self.injections, node_id).get("hard_kill", []):
                        if str(item.raw.get("phase", "during_compute")) != "after_snapshot":
                            continue
                        key = f"{item.id}:after_snapshot:{node_id}"
                        if key in hard_kill_triggered:
                            continue
                        hard_kill_triggered.add(key)
                        _dedupe_injection_hits(
                            snapshot["injections_hit"],
                            injection_hit(item, stage="node_snapshot_committed", effect="hard_kill"),
                        )
                        self._normalize_snapshot(snapshot, graph)
                        self.store.save(snapshot)
                        delay = float(item.raw.get("delay_seconds", 0.0))
                        if delay:
                            time.sleep(delay)
                        os.kill(os.getpid(), signal.SIGKILL)
                if requested_after_wave or controller.requested:
                    break
        finally:
            if pooled is not None:
                pooled.shutdown(wait=True, cancel_futures=False)

        self._resolve_blocked_dependencies(graph, status_map, failures)
        snapshot["node_status"] = status_map
        snapshot["node_products"] = node_products
        snapshot["failures"] = sorted_failures(failures, graph.topo_index)
        snapshot["execution_trace"] = sorted(
            set(execution_trace), key=lambda item: (graph.topo_index[item], item)
        )
        snapshot["completed_nodes"] = [
            item for item in graph.topo_order if status_map.get(item) in SUCCESS_STATUSES
        ]
        snapshot["recalculated_nodes"] = [
            item
            for item in graph.topo_order
            if status_map.get(item) in {"recalculated", "failed"}
        ]
        snapshot["reused_nodes"] = [
            item for item in graph.topo_order if status_map.get(item) == "reused"
        ]
        snapshot["final_outputs"] = self._final_outputs(graph, snapshot)
        snapshot["status"] = "summarizing"
        snapshot["stage"] = "product_summary"
        self._normalize_snapshot(snapshot, graph)
        self.store.save(snapshot)
        snapshot["status"], snapshot["stage"] = self._terminal_status(status_map, failures, controller)
        self._normalize_snapshot(snapshot, graph)
        self.store.save(snapshot)
        report = self._build_report(snapshot, graph)
        return RunOutcome(report=report, snapshot=snapshot, graph=graph, stop_requested=controller.requested)

    def _execute_node(
        self,
        graph: Graph,
        graph_version: int,
        node_id: str,
        input_arrays: dict[str, np.ndarray],
        stable_key: str,
    ) -> dict[str, Any]:
        node = graph.nodes[node_id]
        grouped = node_injections(self.injections, node_id)
        hit_records: list[dict[str, Any]] = []
        delay_ms = float(self.config.raw.get("node_delay_ms", {}).get(node_id, 0.0))
        if delay_ms:
            time.sleep(delay_ms / 1000.0)
        for item in sorted(grouped.get("hard_kill", []), key=lambda value: value.id):
            if str(item.raw.get("phase", "during_compute")) == "after_snapshot":
                continue
            delay = float(item.raw.get("delay_seconds", 0.0))
            if delay:
                time.sleep(delay)
            os.kill(os.getpid(), signal.SIGKILL)

        input_edge = graph.incoming[node_id]
        input_tensor = input_arrays["tensor"]
        input_fp = tensor_fingerprint(input_tensor)
        injection_id: str | None = None
        try:
            for item in grouped.get("shape_mismatch", []):
                expected = tuple(int(value) for value in item.raw["expected_shape"])
                if tuple(input_tensor.shape) != expected:
                    injection_id = item.id
                    hit_records.append(
                        injection_hit(
                            item,
                            stage="parameter_construction",
                            effect="shape_mismatch_raised",
                            expected_shape=list(expected),
                            actual_shape=list(input_tensor.shape),
                        )
                    )
                    raise BusinessError(
                        E_SHAPE_MISMATCH,
                        "injected edge shape mismatch",
                        node_id=node_id,
                        stage="parameter_construction",
                        details={"actual_shape": list(input_tensor.shape), "expected_shape": list(expected)},
                        injection_id=item.id,
                    )
            params = deep_copy_json(node.params)
            for item in grouped.get("padding_out_of_range", []):
                params["padding"] = deep_copy_json(item.raw["padding"])
                injection_id = item.id
                hit_records.append(
                    injection_hit(
                        item,
                        stage="parameter_construction",
                        effect="padding_override_applied",
                        padding=deep_copy_json(item.raw["padding"]),
                    )
                )
            for item in grouped.get("invalid_scale", []):
                params.pop("output_size", None)
                params["scale_factor"] = deep_copy_json(item.raw["scale_factor"])
                injection_id = item.id
                hit_records.append(
                    injection_hit(
                        item,
                        stage="parameter_construction",
                        effect="scale_override_applied",
                        scale_factor=deep_copy_json(item.raw["scale_factor"]),
                    )
                )
            try:
                result = execute_operator(
                    node.op,
                    input_tensor,
                    params,
                    padding_value=self.config.conv_padding_value,
                )
            except BusinessError as exc:
                if injection_id and not exc.injection_id:
                    exc.injection_id = injection_id
                raise
            output = np.ascontiguousarray(result.tensor, dtype=np.float64)
            key = cache_key(node.op, result.normalized_params, input_fp)
            summary = tensor_summary(output)
            self.store.persist_product(key, output)
            record = {
                "node_id": node_id,
                "op": node.op,
                "cache_key": key,
                "product_version": product_version(graph_version, key),
                "graph_version": graph_version,
                "tensor_fingerprint": summary["sha256"],
                "summary": summary,
                "input_fingerprints": {input_edge.tensor: input_fp},
                "input_tensor": input_edge.tensor,
                "operator_metadata": result.metadata,
                "stable_sort_key": stable_key,
                "computed": True,
            }
            return {
                "node_id": node_id,
                "status": "recalculated",
                "record": record,
                "array": output,
                "failure": None,
                "injections": hit_records,
                "stable_sort_key": stable_key,
            }
        except BusinessError as exc:
            failure = exc.to_failure()
            failure["node_id"] = node_id
            if failure.get("stage") in {None, "validation"}:
                failure["stage"] = "parameter_construction"
            if exc.code in {E_NONFINITE_INPUT, E_SHAPE_MISMATCH} and exc.stage == "compute":
                failure["stage"] = "compute"
            return {
                "node_id": node_id,
                "status": "failed",
                "record": None,
                "array": None,
                "failure": failure,
                "injections": hit_records,
                "stable_sort_key": stable_key,
            }
        except Exception as exc:
            return {
                "node_id": node_id,
                "status": "failed",
                "record": None,
                "array": None,
                "failure": failure_from_exception(
                    exc, node_id=node_id, stage="compute", injection_id=injection_id
                ),
                "injections": hit_records,
                "stable_sort_key": stable_key,
            }

    def _collect_input_arrays(
        self,
        graph: Graph,
        node_id: str,
        input_tensor_id: str,
        base_input: np.ndarray,
        product_arrays: dict[str, np.ndarray],
    ) -> dict[str, np.ndarray]:
        edge = graph.incoming[node_id]
        if edge.source == input_tensor_id:
            return {"tensor": base_input}
        if edge.source not in product_arrays:
            raise BusinessError(
                E_DEPENDENCY_FAILED,
                "upstream tensor is unavailable",
                node_id=node_id,
                stage="scheduling",
                details={"source": edge.source},
            )
        return {"tensor": product_arrays[edge.source]}

    def _inputs_available(self, graph: Graph, node_id: str, status_map: dict[str, str]) -> bool:
        source = graph.incoming[node_id].source
        if source not in graph.nodes:
            return True
        return status_map.get(source) in SUCCESS_STATUSES

    def _resolve_blocked_dependencies(
        self,
        graph: Graph,
        status_map: dict[str, str],
        failures: list[dict[str, Any]],
    ) -> None:
        changed = True
        while changed:
            changed = False
            for node_id in graph.topo_order:
                if status_map.get(node_id) != "pending":
                    continue
                if not graph.nodes[node_id].enabled:
                    status_map[node_id] = "skipped_disabled"
                    changed = True
                    continue
                source = graph.incoming[node_id].source
                if source not in graph.nodes:
                    continue
                source_status = status_map.get(source)
                if source_status in BLOCKED_STATUSES:
                    status_map[node_id] = "skipped_dependency"
                    failure = _failure(
                        E_DEPENDENCY_FAILED,
                        "node was skipped because an upstream node did not produce a tensor",
                        node_id=node_id,
                        stage="scheduling",
                        details={"source": source, "source_status": source_status},
                    )
                    if not any(
                        existing.get("node_id") == node_id
                        and existing.get("code") == E_DEPENDENCY_FAILED
                        for existing in failures
                    ):
                        failures.append(failure)
                    changed = True

    def _final_outputs(self, graph: Graph, snapshot: dict[str, Any]) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        records = snapshot.get("node_products", {})
        for output_id in graph.outputs:
            record = records.get(output_id)
            tensor_name = graph.nodes[output_id].tensor
            outputs[output_id] = {
                "node_id": output_id,
                "tensor": tensor_name,
                "product_version": record.get("product_version") if isinstance(record, dict) else None,
                "summary": record.get("summary") if isinstance(record, dict) else None,
            }
        return outputs

    def _terminal_status(
        self,
        status_map: dict[str, str],
        failures: list[dict[str, Any]],
        controller: StopController,
    ) -> tuple[str, str]:
        if controller.requested:
            return "interrupted", "interrupted"
        if failures:
            return "completed_with_failures", "completed_with_failures"
        if any(status == "skipped_disabled" for status in status_map.values()):
            return "completed_with_disabled", "completed_with_disabled"
        if all(status in SUCCESS_STATUSES for status in status_map.values()):
            return "complete", "complete"
        if any(status == "pending" for status in status_map.values()):
            return "interrupted", "interrupted"
        return "completed_with_failures", "completed_with_failures"

    def _normalize_snapshot(self, snapshot: dict[str, Any], graph: Graph) -> None:
        snapshot["completed_nodes"] = [
            node_id
            for node_id in graph.topo_order
            if snapshot.get("node_status", {}).get(node_id) in SUCCESS_STATUSES
        ]
        snapshot["failures"] = sorted_failures(snapshot.get("failures", []), graph.topo_index)
        snapshot["topology"] = graph.topology_records()
        snapshot["topology_index"] = dict(graph.topo_index)

    def _build_report(self, snapshot: dict[str, Any], graph: Graph) -> dict[str, Any]:
        status_map = snapshot.get("node_status", {})
        records = snapshot.get("node_products", {})
        failures = sorted_failures(snapshot.get("failures", []), graph.topo_index)
        failure_by_node: dict[str, list[dict[str, Any]]] = {}
        for failure in failures:
            failure_by_node.setdefault(str(failure.get("node_id")), []).append(failure)
        invalidated_by: dict[str, list[str]] = {}
        for root, chain in snapshot.get("invalidation_chains", {}).items():
            if not isinstance(chain, list):
                continue
            for node_id in chain:
                invalidated_by.setdefault(node_id, []).append(root)
        node_results: list[dict[str, Any]] = []
        for node_id in graph.topo_order:
            node = graph.nodes[node_id]
            record = records.get(node_id) if isinstance(records, dict) else None
            status = status_map.get(node_id, "pending")
            node_results.append(
                {
                    "node_id": node_id,
                    "topology_index": graph.topo_index[node_id],
                    "stable_sort_key": stable_sort_key(graph.topo_index[node_id], node_id),
                    "op": node.op,
                    "tensor": node.tensor,
                    "status": status,
                    "recalculated": status in {"recalculated", "failed"},
                    "reused": status == "reused",
                    "product_version": record.get("product_version") if isinstance(record, dict) else None,
                    "cache_key": record.get("cache_key") if isinstance(record, dict) else None,
                    "tensor_summary": record.get("summary") if isinstance(record, dict) else None,
                    "operator_metadata": record.get("operator_metadata") if isinstance(record, dict) else None,
                    "input_fingerprints": record.get("input_fingerprints") if isinstance(record, dict) else None,
                    "failures": failure_by_node.get(node_id, []),
                    "invalidated_by": sorted(set(invalidated_by.get(node_id, []))),
                }
            )
        recalculated = [item["node_id"] for item in node_results if item["recalculated"]]
        reused = [item["node_id"] for item in node_results if item["reused"]]
        business_codes = sorted({str(item.get("code")) for item in failures})
        status = str(snapshot.get("status", "failed"))
        exit_code = 0 if status in {"complete", "completed_with_disabled", "interrupted"} else 2
        return {
            "report_version": 1,
            "status": status,
            "exit_code": exit_code,
            "graph_id": graph.graph_id,
            "graph_version": int(snapshot.get("graph_version", 0)),
            "graph_fingerprint": snapshot.get("graph_fingerprint"),
            "graph_semantic_fingerprint": graph.fingerprint,
            "config_hash": snapshot.get("config_hash"),
            "snapshot_version": snapshot.get("snapshot_version"),
            "snapshot_phase": snapshot.get("phase_seq"),
            "snapshot_stage": snapshot.get("stage"),
            "input": {
                **snapshot.get("input_summary", {}),
                "fingerprint": snapshot.get("input_fingerprint"),
                "descriptor_hash": snapshot.get("input_descriptor_hash"),
            },
            "topology": graph.topology_records(),
            "max_workers": self.config.max_workers,
            "completed_nodes": list(snapshot.get("completed_nodes", [])),
            "nodes": node_results,
            "recalculated_nodes": recalculated,
            "reused_nodes": reused,
            "invalidated_nodes": sorted(
                set(snapshot.get("invalidated_nodes", [])),
                key=lambda item: graph.topo_index.get(item, 10**9),
            ),
            "invalidation_chains": snapshot.get("invalidation_chains", {}),
            "removed_nodes": snapshot.get("removed_nodes", []),
            "execution_trace": snapshot.get("execution_trace", []),
            "final_outputs": snapshot.get("final_outputs", {}),
            "failures": failures,
            "business_codes": business_codes,
            "injections_hit": snapshot.get("injections_hit", []),
            "edit_history": snapshot.get("edit_history", []),
        }

    def _invalid_graph_outcome(
        self,
        snapshot: dict[str, Any],
        validation: ValidationResult,
        controller: StopController,
    ) -> RunOutcome:
        snapshot["failures"] = sorted_failures(validation.failures, {})
        snapshot["status"] = "invalid_graph"
        snapshot["stage"] = "graph_validation"
        snapshot["node_status"] = {}
        snapshot["node_products"] = {}
        snapshot["final_outputs"] = {}
        report = {
            "report_version": 1,
            "status": "invalid_graph",
            "exit_code": 2,
            "graph_version": int(snapshot.get("graph_version", 0)),
            "graph_fingerprint": snapshot.get("graph_fingerprint"),
            "config_hash": snapshot.get("config_hash"),
            "topology": [],
            "nodes": [],
            "recalculated_nodes": [],
            "reused_nodes": [],
            "invalidated_nodes": [],
            "invalidation_chains": {},
            "removed_nodes": [],
            "failures": snapshot["failures"],
            "business_codes": sorted({str(item.get("code")) for item in snapshot["failures"]}),
            "injections_hit": snapshot.get("injections_hit", []),
        }
        return RunOutcome(report=report, snapshot=snapshot, graph=None, stop_requested=controller.requested)

    def commit_edit(self, snapshot: dict[str, Any], operation: dict[str, Any]) -> EditOutcome:
        snapshot = deep_copy_json(snapshot)
        old_raw = snapshot.get("graph")
        old_validation = validate_graph(old_raw)
        if old_validation.graph is None or old_validation.failures:
            raise BusinessError(E_GRAPH_INVALID, "current state graph is invalid", stage="edit")
        old_graph = old_validation.graph
        edit_info = {
            "id": operation.get("id", f"edit-{operation.get('op', 'unknown')}"),
            "op": operation.get("op"),
            "description": operation.get("description", ""),
            "from_version": int(snapshot.get("graph_version", 0)),
            "to_version": int(snapshot.get("graph_version", 0)) + 1,
        }
        try:
            new_raw, applied_info = apply_edit_operation(old_raw, operation)
            edit_info.update(applied_info)
            edit_info["from_version"] = int(snapshot.get("graph_version", 0))
            edit_info["to_version"] = int(snapshot.get("graph_version", 0)) + 1
            validation = validate_graph(new_raw)
        except BusinessError as exc:
            return EditOutcome(
                committed=False,
                snapshot=snapshot,
                graph=old_graph,
                failures=[exc.to_failure()],
                edit_info=edit_info,
                invalidated_nodes=[],
                reused_nodes=[],
                removed_nodes=[],
                invalidation_chains={},
            )
        if validation.graph is None or validation.failures:
            return EditOutcome(
                committed=False,
                snapshot=snapshot,
                graph=validation.graph,
                failures=validation.failures,
                edit_info=edit_info,
                invalidated_nodes=[],
                reused_nodes=[],
                removed_nodes=[],
                invalidation_chains={},
            )
        new_graph = validation.graph
        new_version = int(snapshot.get("graph_version", 0)) + 1
        old_ids = set(old_graph.nodes)
        new_ids = set(new_graph.nodes)
        roots: set[str] = set()
        roots.update(new_ids - old_ids)
        removed = old_ids - new_ids
        if old_graph.input_tensors != new_graph.input_tensors:
            roots.update(new_ids)
        for node_id in sorted(old_ids & new_ids):
            old_semantic = old_graph.nodes[node_id].semantic()
            new_semantic = new_graph.nodes[node_id].semantic()
            old_incoming = old_graph.incoming.get(node_id)
            new_incoming = new_graph.incoming.get(node_id)
            old_source = (old_incoming.source, old_incoming.tensor, old_incoming.id) if old_incoming else None
            new_source = (new_incoming.source, new_incoming.tensor, new_incoming.id) if new_incoming else None
            if old_semantic != new_semantic or old_source != new_source:
                roots.add(node_id)
        for removed_id in removed:
            for descendant in old_graph.descendants({removed_id}, include_roots=False):
                if descendant in new_ids:
                    roots.add(descendant)

        old_records = snapshot.get("node_products", {}) if isinstance(snapshot.get("node_products"), dict) else {}
        valid_records: dict[str, dict[str, Any]] = {}
        for node_id in sorted(old_ids & new_ids):
            record = old_records.get(node_id)
            if record is None or not isinstance(record, dict):
                roots.add(node_id)
                continue
            if self.store.load_product(record) is None:
                roots.add(node_id)
                continue
            valid_records[node_id] = record
        invalidated = new_graph.descendants(roots) if roots else []
        if roots:
            for node_id in invalidated:
                valid_records.pop(node_id, None)
        elif [node_id for node_id in new_graph.topo_order if node_id in valid_records] != list(new_graph.topo_order):
            for node_id in new_graph.topo_order:
                if node_id not in valid_records:
                    roots.add(node_id)
            invalidated = new_graph.descendants(roots)
            for node_id in invalidated:
                valid_records.pop(node_id, None)

        chains: dict[str, list[str]] = {}
        for root_id in sorted(roots, key=lambda item: (new_graph.topo_index.get(item, 10**9), item)):
            chain: list[str] = []
            if root_id in new_ids:
                chain = new_graph.descendants({root_id})
            if root_id in removed:
                for descendant in old_graph.descendants({root_id}, include_roots=False):
                    if descendant in new_ids:
                        candidate = new_graph.descendants({descendant})
                        for item in candidate:
                            if item not in chain:
                                chain.append(item)
            chains[root_id] = [item for item in new_graph.topo_order if item in set(chain)]

        reused_records: dict[str, dict[str, Any]] = {}
        for node_id, record in valid_records.items():
            copied = deep_copy_json(record)
            copied["reused_from_version"] = record.get("product_version")
            copied["product_version"] = product_version(new_version, str(record["cache_key"]))
            copied["graph_version"] = new_version
            copied["computed"] = False
            reused_records[node_id] = copied
        reused_nodes = [node_id for node_id in new_graph.topo_order if node_id in reused_records]
        invalidated = [node_id for node_id in new_graph.topo_order if node_id in set(invalidated)]
        removed_nodes = sorted(removed)
        status_map = {
            node_id: "pending"
            for node_id in new_graph.topo_order
        }
        for node_id in reused_nodes:
            status_map[node_id] = "reused"
        for node_id in new_graph.topo_order:
            if not new_graph.nodes[node_id].enabled:
                status_map[node_id] = "skipped_disabled"

        snapshot["graph"] = new_raw
        snapshot["graph_version"] = new_version
        snapshot["graph_fingerprint"] = sha256_json(new_raw)
        snapshot["topology"] = new_graph.topology_records()
        snapshot["topology_index"] = dict(new_graph.topo_index)
        snapshot["node_products"] = reused_records
        snapshot["completed_nodes"] = reused_nodes
        snapshot["node_status"] = status_map
        snapshot["failures"] = []
        snapshot["injections_hit"] = []
        snapshot["execution_trace"] = []
        snapshot["final_outputs"] = {}
        snapshot["invalidated_nodes"] = invalidated
        snapshot["invalidation_chains"] = chains
        snapshot["removed_nodes"] = removed_nodes
        snapshot["recalculated_nodes"] = invalidated
        snapshot["reused_nodes"] = reused_nodes
        snapshot["status"] = "scheduled"
        snapshot["stage"] = "topology_scheduled"
        edits = snapshot.setdefault("edit_history", [])
        edits.append(edit_info)
        self._normalize_snapshot(snapshot, new_graph)
        self.store.save(snapshot)
        return EditOutcome(
            committed=True,
            snapshot=snapshot,
            graph=new_graph,
            failures=[],
            edit_info=edit_info,
            invalidated_nodes=invalidated,
            reused_nodes=reused_nodes,
            removed_nodes=removed_nodes,
            invalidation_chains=chains,
        )
