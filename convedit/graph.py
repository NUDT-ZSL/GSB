"""Operator graph parsing, validation, content fingerprints, and scheduling."""

from __future__ import annotations

import copy
import heapq
import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from .config import ALIGN_MODES, OPERATOR_IMPLEMENTATION_VERSION, SUPPORTED_OPS
from .errors import (
    BusinessError,
    E_GRAPH_INVALID,
    E_PARAM_OUT_OF_RANGE,
)
from .util import load_json, sha256_json, stable_sort_key


MAX_KERNEL = 31
MAX_STRIDE = 64
MAX_PADDING = 64
MAX_CHANNELS = 1024
MAX_INTERPOLATION_SIZE = 16384
MAX_SCALE_FACTOR = 64.0
ABSOLUTE_TENSOR_SIZE = 100_000_000


@dataclass(frozen=True)
class Node:
    id: str
    op: str
    tensor: str
    params: dict[str, Any]
    enabled: bool = True

    def semantic(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "op": self.op,
            "tensor": self.tensor,
            "enabled": self.enabled,
            "params": self.params,
        }

    def parameter_fingerprint(self) -> str:
        return sha256_json(
            {
                "op": self.op,
                "params": self.params,
                "enabled": self.enabled,
                "operator_version": OPERATOR_IMPLEMENTATION_VERSION,
            }
        )


@dataclass(frozen=True)
class Edge:
    id: str
    source: str
    target: str
    tensor: str

    def semantic(self) -> dict[str, str]:
        return {
            "id": self.id,
            "from": self.source,
            "to": self.target,
            "tensor": self.tensor,
        }


@dataclass
class Graph:
    graph_id: str
    input_tensors: dict[str, dict[str, Any]]
    nodes: dict[str, Node]
    edges: list[Edge]
    outputs: list[str]
    schema_version: int
    topo_order: list[str] = field(default_factory=list)
    topo_index: dict[str, int] = field(default_factory=dict)
    incoming: dict[str, Edge] = field(default_factory=dict)
    children: dict[str, list[str]] = field(default_factory=dict)
    fingerprint: str = ""

    def semantic(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "graph_id": self.graph_id,
            "input_tensors": self.input_tensors,
            "nodes": [self.nodes[node_id].semantic() for node_id in self.topo_order],
            "edges": [
                edge.semantic()
                for edge in sorted(self.edges, key=lambda item: (item.id, item.source, item.target, item.tensor))
            ],
            "outputs": self.outputs,
            "operator_implementation_version": OPERATOR_IMPLEMENTATION_VERSION,
        }

    def node_schedule(self, node_id: str) -> dict[str, Any]:
        index = self.topo_index[node_id]
        return {
            "index": index,
            "node_id": node_id,
            "stable_sort_key": stable_sort_key(index, node_id),
            "op": self.nodes[node_id].op,
        }

    def topology_records(self) -> list[dict[str, Any]]:
        return [self.node_schedule(node_id) for node_id in self.topo_order]

    def input_tensor_names_for(self, node_id: str) -> list[str]:
        edge = self.incoming.get(node_id)
        return [edge.tensor] if edge else []

    def descendants(self, roots: set[str], *, include_roots: bool = True) -> list[str]:
        seen: set[str] = set()
        stack = list(roots)
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            stack.extend(self.children.get(current, []))
        if not include_roots:
            seen.difference_update(roots)
        return [node_id for node_id in self.topo_order if node_id in seen]

    def ancestors(self, node_id: str) -> list[str]:
        result: set[str] = set()
        current = node_id
        while current in self.incoming:
            source = self.incoming[current].source
            if source not in self.nodes:
                break
            result.add(source)
            current = source
        return [item for item in self.topo_order if item in result]


@dataclass
class ValidationResult:
    graph: Graph | None
    failures: list[dict[str, Any]]
    structural_valid: bool
    parameter_valid: bool

    @property
    def ok(self) -> bool:
        return self.structural_valid and self.parameter_valid and not self.failures


def _failure(code: str, message: str, *, node_id: str | None = None, details: dict[str, Any] | None = None) -> dict[str, Any]:
    record: dict[str, Any] = {
        "code": code,
        "message": message,
        "node_id": node_id,
        "stage": "graph_validation",
        "exception_type": "BusinessError",
    }
    if details:
        record["details"] = details
    return record


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _vector2(value: Any, *, field: str, node_id: str, integer: bool, minimum: float, maximum: float) -> list[Any]:
    if not isinstance(value, list) or len(value) != 2:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} must be a two-element array",
            node_id=node_id,
            details={"field": field, "value": value},
        )
    result: list[Any] = []
    for index, item in enumerate(value):
        if integer:
            if not _is_int(item):
                raise BusinessError(
                    E_PARAM_OUT_OF_RANGE,
                    f"{field}[{index}] must be an integer",
                    node_id=node_id,
                    details={"field": field, "value": item},
                )
        elif not _is_number(item) or not math.isfinite(float(item)):
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"{field}[{index}] must be a finite number",
                node_id=node_id,
                details={"field": field, "value": item},
            )
        numeric = int(item) if integer else float(item)
        if not minimum <= numeric <= maximum:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"{field}[{index}] must be in [{minimum}, {maximum}]",
                node_id=node_id,
                details={"field": field, "value": item, "minimum": minimum, "maximum": maximum},
            )
        result.append(numeric)
    return result


def _integer(value: Any, *, field: str, node_id: str, minimum: int, maximum: int) -> int:
    if not _is_int(value):
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} must be an integer",
            node_id=node_id,
            details={"field": field, "value": value},
        )
    if not minimum <= value <= maximum:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} must be in [{minimum}, {maximum}]",
            node_id=node_id,
            details={"field": field, "value": value, "minimum": minimum, "maximum": maximum},
        )
    return int(value)


def _numeric_array(value: Any, *, field: str, node_id: str, shape: tuple[int, ...] | None = None) -> list[Any]:
    try:
        array = np.asarray(value, dtype=np.float64)
    except (TypeError, ValueError) as exc:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} must be a rectangular numeric array",
            node_id=node_id,
            details={"field": field, "reason": str(exc)},
        ) from exc
    if shape is not None and array.shape != shape:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} must have shape {list(shape)}",
            node_id=node_id,
            details={"field": field, "actual_shape": list(array.shape), "expected_shape": list(shape)},
        )
    if array.size > ABSOLUTE_TENSOR_SIZE:
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} is too large",
            node_id=node_id,
            details={"field": field, "size": int(array.size)},
        )
    if not np.isfinite(array).all():
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            f"{field} contains non-finite values",
            node_id=node_id,
            details={"field": field},
        )
    return array.astype("<f8").tolist()


def validate_node_params(op: str, raw_params: Any, *, node_id: str) -> dict[str, Any]:
    if not isinstance(raw_params, dict):
        raise BusinessError(
            E_PARAM_OUT_OF_RANGE,
            "params must be a JSON object",
            node_id=node_id,
            details={"node_id": node_id},
        )
    if op == "conv2d":
        allowed = {"kernel_size", "stride", "padding", "in_channels", "out_channels", "weights", "bias"}
        unknown = sorted(set(raw_params) - allowed)
        if unknown:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "unknown conv2d parameter(s)",
                node_id=node_id,
                details={"unknown": unknown},
            )
        missing = sorted({"kernel_size", "stride", "padding", "in_channels", "out_channels", "weights"} - set(raw_params))
        if missing:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "missing conv2d parameter(s)",
                node_id=node_id,
                details={"missing": missing},
            )
        kernel = _vector2(
            raw_params["kernel_size"], field="kernel_size", node_id=node_id, integer=True, minimum=1, maximum=MAX_KERNEL
        )
        stride = _vector2(
            raw_params["stride"], field="stride", node_id=node_id, integer=True, minimum=1, maximum=MAX_STRIDE
        )
        padding = _vector2(
            raw_params["padding"], field="padding", node_id=node_id, integer=True, minimum=0, maximum=MAX_PADDING
        )
        in_channels = _integer(
            raw_params["in_channels"], field="in_channels", node_id=node_id, minimum=1, maximum=MAX_CHANNELS
        )
        out_channels = _integer(
            raw_params["out_channels"], field="out_channels", node_id=node_id, minimum=1, maximum=MAX_CHANNELS
        )
        weights = _numeric_array(
            raw_params["weights"],
            field="weights",
            node_id=node_id,
            shape=(out_channels, in_channels, int(kernel[0]), int(kernel[1])),
        )
        if "bias" in raw_params and raw_params["bias"] is not None:
            bias = _numeric_array(raw_params["bias"], field="bias", node_id=node_id, shape=(out_channels,))
        else:
            bias = [0.0] * out_channels
        return {
            "kernel_size": kernel,
            "stride": stride,
            "padding": padding,
            "in_channels": in_channels,
            "out_channels": out_channels,
            "weights": weights,
            "bias": bias,
        }
    if op in {"bilinear", "nearest"}:
        allowed = {"output_size", "scale_factor", "align_mode", "rounding_rule", "boundary"}
        unknown = sorted(set(raw_params) - allowed)
        if unknown:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"unknown {op} parameter(s)",
                node_id=node_id,
                details={"unknown": unknown},
            )
        has_size = "output_size" in raw_params and raw_params["output_size"] is not None
        has_scale = "scale_factor" in raw_params and raw_params["scale_factor"] is not None
        if has_size == has_scale:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "exactly one of output_size and scale_factor must be provided",
                node_id=node_id,
                details={"has_output_size": has_size, "has_scale_factor": has_scale},
            )
        normalized: dict[str, Any] = {
            "align_mode": raw_params.get("align_mode", "half_pixel"),
        }
        if normalized["align_mode"] not in ALIGN_MODES:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "align_mode must be one of half_pixel, asymmetric, align_corners",
                node_id=node_id,
                details={"align_mode": normalized["align_mode"]},
            )
        if has_size:
            normalized["output_size"] = _vector2(
                raw_params["output_size"],
                field="output_size",
                node_id=node_id,
                integer=True,
                minimum=1,
                maximum=MAX_INTERPOLATION_SIZE,
            )
        else:
            normalized["scale_factor"] = _vector2(
                raw_params["scale_factor"],
                field="scale_factor",
                node_id=node_id,
                integer=False,
                minimum=0.000001,
                maximum=MAX_SCALE_FACTOR,
            )
        fixed_rounding = "floor_ceil" if op == "bilinear" else "floor"
        if "rounding_rule" in raw_params and raw_params["rounding_rule"] not in {None, fixed_rounding}:
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                f"{op} rounding_rule is fixed to {fixed_rounding}",
                node_id=node_id,
                details={"rounding_rule": raw_params["rounding_rule"], "expected": fixed_rounding},
            )
        normalized["rounding_rule"] = fixed_rounding
        if raw_params.get("boundary", "clip") != "clip":
            raise BusinessError(
                E_PARAM_OUT_OF_RANGE,
                "interpolation boundary is fixed to clip",
                node_id=node_id,
                details={"boundary": raw_params.get("boundary")},
            )
        normalized["boundary"] = "clip"
        return normalized
    raise BusinessError(E_GRAPH_INVALID, f"unsupported operator {op!r}", node_id=node_id)


def validate_external_inputs(raw_inputs: Any) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    failures: list[dict[str, Any]] = []
    if not isinstance(raw_inputs, dict) or not raw_inputs:
        return {}, [_failure(E_GRAPH_INVALID, "input_tensors must be a non-empty object")]
    normalized: dict[str, dict[str, Any]] = {}
    for tensor_id, spec in raw_inputs.items():
        if not isinstance(tensor_id, str) or not tensor_id:
            failures.append(_failure(E_GRAPH_INVALID, "input tensor ID must be a non-empty string"))
            continue
        if not isinstance(spec, dict):
            failures.append(_failure(E_GRAPH_INVALID, f"input tensor {tensor_id!r} metadata must be an object"))
            continue
        shape = spec.get("shape")
        dtype = spec.get("dtype", "float64")
        if (
            not isinstance(shape, list)
            or len(shape) != 4
            or any(not _is_int(item) or item < 1 for item in shape)
            or int(np.prod(shape)) > ABSOLUTE_TENSOR_SIZE
        ):
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"input tensor {tensor_id!r} shape must be four positive NCHW dimensions",
                    details={"tensor": tensor_id, "shape": shape},
                )
            )
            continue
        if dtype != "float64":
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"input tensor {tensor_id!r} dtype must be float64",
                    details={"tensor": tensor_id, "dtype": dtype},
                )
            )
            continue
        normalized[tensor_id] = {"shape": [int(item) for item in shape], "dtype": "float64"}
    return normalized, failures


def validate_graph(raw: Any) -> ValidationResult:
    failures: list[dict[str, Any]] = []
    if not isinstance(raw, dict):
        return ValidationResult(None, [_failure(E_GRAPH_INVALID, "graph must be a JSON object")], False, False)
    schema_version = raw.get("schema_version", 1)
    if not _is_int(schema_version) or schema_version != 1:
        failures.append(_failure(E_GRAPH_INVALID, "schema_version must be 1"))
    graph_id = raw.get("graph_id")
    if not isinstance(graph_id, str) or not graph_id:
        failures.append(_failure(E_GRAPH_INVALID, "graph_id must be a non-empty string"))
    input_tensors, input_failures = validate_external_inputs(raw.get("input_tensors"))
    failures.extend(input_failures)
    raw_nodes = raw.get("nodes")
    raw_edges = raw.get("edges")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        failures.append(_failure(E_GRAPH_INVALID, "nodes must be a non-empty array"))
        raw_nodes = []
    if not isinstance(raw_edges, list):
        failures.append(_failure(E_GRAPH_INVALID, "edges must be an array"))
        raw_edges = []

    node_specs: dict[str, dict[str, Any]] = {}
    node_order: list[str] = []
    node_tensor_owner: dict[str, str] = {}
    for position, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            failures.append(_failure(E_GRAPH_INVALID, f"nodes[{position}] must be an object"))
            continue
        node_id = raw_node.get("id")
        if not isinstance(node_id, str) or not node_id:
            failures.append(_failure(E_GRAPH_INVALID, f"nodes[{position}].id must be a non-empty string"))
            continue
        if node_id in node_specs:
            failures.append(_failure(E_GRAPH_INVALID, f"duplicate node ID {node_id!r}", node_id=node_id))
            continue
        op = raw_node.get("op")
        if op not in SUPPORTED_OPS:
            failures.append(
                _failure(E_GRAPH_INVALID, f"unsupported op {op!r}", node_id=node_id, details={"op": op})
            )
        tensor = raw_node.get("tensor", node_id)
        if not isinstance(tensor, str) or not tensor:
            failures.append(_failure(E_GRAPH_INVALID, "node tensor must be a non-empty string", node_id=node_id))
            tensor = node_id
        if tensor in input_tensors or tensor in node_tensor_owner:
            other = node_tensor_owner.get(tensor, "external input")
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"tensor {tensor!r} has more than one producer",
                    node_id=node_id,
                    details={"tensor": tensor, "other_producer": other},
                )
            )
        else:
            node_tensor_owner[tensor] = node_id
        enabled = raw_node.get("enabled", True)
        if not isinstance(enabled, bool):
            failures.append(_failure(E_GRAPH_INVALID, "enabled must be boolean", node_id=node_id))
            enabled = True
        params = raw_node.get("params", {})
        node_specs[node_id] = {
            "id": node_id,
            "op": op if isinstance(op, str) else str(op),
            "tensor": tensor,
            "params": params,
            "enabled": enabled,
        }
        node_order.append(node_id)

    edge_ids: set[str] = set()
    edge_triples: set[tuple[str, str, str]] = set()
    edges: list[Edge] = []
    incoming_count: dict[str, int] = {}
    incoming_edge: dict[str, Edge] = {}
    for position, raw_edge in enumerate(raw_edges):
        if not isinstance(raw_edge, dict):
            failures.append(_failure(E_GRAPH_INVALID, f"edges[{position}] must be an object"))
            continue
        edge_id = raw_edge.get("id")
        if edge_id is None:
            edge_id = f"auto-edge-{position}"
        if not isinstance(edge_id, str) or not edge_id:
            failures.append(_failure(E_GRAPH_INVALID, f"edges[{position}].id must be a non-empty string"))
            continue
        if edge_id in edge_ids:
            failures.append(_failure(E_GRAPH_INVALID, f"duplicate edge ID {edge_id!r}", details={"edge_id": edge_id}))
            continue
        source = raw_edge.get("from", raw_edge.get("source"))
        target = raw_edge.get("to", raw_edge.get("target"))
        tensor = raw_edge.get("tensor")
        if not isinstance(source, str) or not isinstance(target, str) or not isinstance(tensor, str) or not tensor:
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    "edge requires string from, to, and tensor fields",
                    details={"edge_id": edge_id},
                )
            )
            continue
        if source not in input_tensors and source not in node_specs:
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"edge {edge_id!r} source {source!r} does not exist",
                    details={"edge_id": edge_id},
                )
            )
        if target not in node_specs:
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"edge {edge_id!r} target {target!r} does not exist",
                    details={"edge_id": edge_id},
                )
            )
        if source in node_specs and node_specs[source]["tensor"] != tensor:
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"edge {edge_id!r} tensor does not match source node output",
                    node_id=source,
                    details={"edge_id": edge_id, "edge_tensor": tensor, "source_tensor": node_specs[source]["tensor"]},
                )
            )
        if source in input_tensors and tensor != source:
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    f"edge {edge_id!r} tensor does not match external input ID",
                    details={"edge_id": edge_id, "edge_tensor": tensor, "source": source},
                )
            )
        triple = (source, target, tensor)
        if triple in edge_triples:
            failures.append(_failure(E_GRAPH_INVALID, "duplicate edge", details={"edge_id": edge_id}))
        edge_triples.add(triple)
        edge_ids.add(edge_id)
        incoming_count[target] = incoming_count.get(target, 0) + 1
        edge = Edge(id=edge_id, source=source, target=target, tensor=tensor)
        incoming_edge[target] = edge
        edges.append(edge)

    for node_id in node_specs:
        count = incoming_count.get(node_id, 0)
        if count != 1:
            failures.append(
                _failure(
                    E_GRAPH_INVALID,
                    "each unary operator node must have exactly one incoming tensor edge",
                    node_id=node_id,
                    details={"incoming_edges": count},
                )
            )

    outputs = raw.get("outputs")
    normalized_outputs: list[str] = []
    if not isinstance(outputs, list) or not outputs:
        failures.append(_failure(E_GRAPH_INVALID, "outputs must be a non-empty array"))
    else:
        for output in outputs:
            if not isinstance(output, str) or output not in node_specs:
                failures.append(
                    _failure(E_GRAPH_INVALID, f"output node {output!r} does not exist", details={"output": output})
                )
            elif output not in normalized_outputs:
                normalized_outputs.append(output)

    structural_valid = not failures
    if not structural_valid:
        return ValidationResult(None, failures, False, False)

    children: dict[str, list[str]] = {node_id: [] for node_id in node_specs}
    indegree = {node_id: 0 for node_id in node_specs}
    for edge in edges:
        if edge.source in node_specs:
            children[edge.source].append(edge.target)
            indegree[edge.target] += 1
    for values in children.values():
        values.sort()
    heap = [node_id for node_id, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    topo_order: list[str] = []
    while heap:
        current = heapq.heappop(heap)
        topo_order.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                heapq.heappush(heap, child)
    if len(topo_order) != len(node_specs):
        cyclic = sorted(node_id for node_id, degree in indegree.items() if degree > 0)
        return ValidationResult(
            None,
            [_failure(E_GRAPH_INVALID, "graph contains a cycle", details={"cycle_nodes": cyclic})],
            False,
            False,
        )

    param_failures: list[dict[str, Any]] = []
    nodes: dict[str, Node] = {}
    for node_id in topo_order:
        spec = node_specs[node_id]
        try:
            normalized_params = validate_node_params(spec["op"], spec["params"], node_id=node_id)
        except BusinessError as exc:
            record = exc.to_failure()
            record["stage"] = "parameter_construction"
            param_failures.append(record)
            normalized_params = copy.deepcopy(spec["params"])
        nodes[node_id] = Node(
            id=node_id,
            op=spec["op"],
            tensor=spec["tensor"],
            params=normalized_params,
            enabled=spec["enabled"],
        )

    topo_index = {node_id: index for index, node_id in enumerate(topo_order)}
    graph = Graph(
        graph_id=graph_id,
        input_tensors=input_tensors,
        nodes=nodes,
        edges=edges,
        outputs=normalized_outputs,
        schema_version=schema_version,
        topo_order=topo_order,
        topo_index=topo_index,
        incoming=incoming_edge,
        children=children,
    )
    graph.fingerprint = sha256_json(graph.semantic())
    return ValidationResult(graph, param_failures, True, not param_failures)


def load_graph_file(path: Path) -> ValidationResult:
    try:
        raw = load_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        return ValidationResult(
            None,
            [_failure(E_GRAPH_INVALID, f"cannot load graph JSON: {exc}", details={"path": str(path)})],
            False,
            False,
        )
    return validate_graph(raw)


def graph_from_raw_or_raise(raw: Any) -> Graph:
    result = validate_graph(raw)
    if not result.ok or result.graph is None:
        first = result.failures[0] if result.failures else {"code": E_GRAPH_INVALID, "message": "invalid graph"}
        raise BusinessError(
            first["code"],
            first["message"],
            node_id=first.get("node_id"),
            stage=first.get("stage", "graph_validation"),
            details=first.get("details"),
        )
    return result.graph
