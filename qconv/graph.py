"""Operator graph model, normalization and structural validation."""

from __future__ import annotations

import copy
import heapq
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .errors import Failure
from .util import canonical_json, finite_positive, sha256_bytes, sha256_json


STRUCTURAL_CODE = "E_GRAPH_INVALID"
QUANT_CODE = "E_QUANT_PARAM"


@dataclass
class GraphInput:
    name: str
    shape: tuple[int, int, int, int]
    pack: str
    bit_width: int
    scale: float
    dtype: str = "int8"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "shape": list(self.shape),
            "pack": self.pack,
            "bit_width": self.bit_width,
            "scale": self.scale,
            "dtype": self.dtype,
        }


@dataclass
class QuantNode:
    id: str
    input_tensor: str
    output_tensor: str
    in_channels: int
    out_channels: int
    kernel: tuple[int, int]
    stride: tuple[int, int]
    padding: tuple[int, int]
    weight_pack: str
    weight_shape: tuple[int, int, int, int]
    weight_bit_width: int
    weight_scale: float
    activation_bit_width: int
    activation_scale: float
    accumulator_bits: int = 32
    overflow_policy: str = "error"

    def clone(self) -> "QuantNode":
        return copy.deepcopy(self)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "input": self.input_tensor,
            "output": self.output_tensor,
            "in_channels": self.in_channels,
            "out_channels": self.out_channels,
            "kernel": list(self.kernel),
            "stride": list(self.stride),
            "padding": list(self.padding),
            "weight_pack": self.weight_pack,
            "weight_shape": list(self.weight_shape),
            "weight_bit_width": self.weight_bit_width,
            "weight_scale": self.weight_scale,
            "activation_bit_width": self.activation_bit_width,
            "activation_scale": self.activation_scale,
            "accumulator_bits": self.accumulator_bits,
            "overflow_policy": self.overflow_policy,
        }

    def cache_parameters(self) -> dict[str, Any]:
        return {
            "input": self.input_tensor,
            "output": self.output_tensor,
            "in_channels": self.in_channels,
            "out_channels": self.out_channels,
            "kernel": list(self.kernel),
            "stride": list(self.stride),
            "padding": list(self.padding),
            "weight_pack": self.weight_pack,
            "weight_shape": list(self.weight_shape),
            "weight_bit_width": self.weight_bit_width,
            "weight_scale": self.weight_scale,
            "activation_bit_width": self.activation_bit_width,
            "activation_scale": self.activation_scale,
            "accumulator_bits": self.accumulator_bits,
            "overflow_policy": self.overflow_policy,
        }


@dataclass(frozen=True)
class Edge:
    source: str
    destination: str

    def to_dict(self) -> dict[str, str]:
        return {"from": self.source, "to": self.destination}


@dataclass
class Graph:
    input: GraphInput
    nodes: dict[str, QuantNode]
    edges: tuple[Edge, ...]
    outputs: list[str]
    graph_version: int = 1
    schema_version: int = 1
    topology: tuple[str, ...] = field(default_factory=tuple)
    levels: dict[str, int] = field(default_factory=dict)
    output_shapes: dict[str, tuple[int, int, int, int]] = field(default_factory=dict)

    def refresh_topology(self) -> None:
        predecessors: dict[str, set[str]] = {node_id: set() for node_id in self.nodes}
        producer: dict[str, str | None] = {self.input.name: None}
        for node in self.nodes.values():
            producer[node.output_tensor] = node.id
        for edge in self.edges:
            source_node = producer.get(edge.source)
            if source_node is not None:
                predecessors[edge.destination].add(source_node)

        indegree = {node_id: len(preds) for node_id, preds in predecessors.items()}
        successors: dict[str, set[str]] = {node_id: set() for node_id in self.nodes}
        for node_id, preds in predecessors.items():
            for pred in preds:
                successors[pred].add(node_id)
        ready = [node_id for node_id, degree in indegree.items() if degree == 0]
        heapq.heapify(ready)
        kahn: list[str] = []
        while ready:
            current = heapq.heappop(ready)
            kahn.append(current)
            for nxt in sorted(successors[current]):
                indegree[nxt] -= 1
                if indegree[nxt] == 0:
                    heapq.heappush(ready, nxt)
        levels: dict[str, int] = {}
        for node_id in kahn:
            preds = predecessors[node_id]
            levels[node_id] = 0 if not preds else 1 + max(levels[pred] for pred in preds)
        self.levels = levels
        self.topology = tuple(sorted(kahn, key=lambda node_id: (levels[node_id], node_id)))
        shapes: dict[str, tuple[int, int, int, int]] = {self.input.name: self.input.shape}
        for node_id in self.topology:
            node = self.nodes[node_id]
            batch, _, height, width = shapes.get(node.input_tensor, self.input.shape)
            out_h = (height + 2 * node.padding[0] - node.kernel[0]) // node.stride[0] + 1
            out_w = (width + 2 * node.padding[1] - node.kernel[1]) // node.stride[1] + 1
            shapes[node.output_tensor] = (batch, node.out_channels, out_h, out_w)
        self.output_shapes = shapes

    def sort_key(self, node_id: str) -> str:
        try:
            rank = self.topology.index(node_id)
        except ValueError:
            return f"99999999:{node_id}"
        return f"{rank:08d}:{node_id}"

    def sorted_nodes(self, node_ids: Iterable[str] | None = None) -> list[str]:
        selected = set(self.nodes if node_ids is None else node_ids)
        return [node_id for node_id in self.topology if node_id in selected]

    def predecessors(self, node_id: str) -> set[str]:
        incoming = {edge.source for edge in self.edges if edge.destination == node_id}
        result: set[str] = set()
        for node in self.nodes.values():
            if node.output_tensor in incoming:
                result.add(node.id)
        return result

    def successors(self, node_id: str) -> set[str]:
        output = self.nodes[node_id].output_tensor
        return {edge.destination for edge in self.edges if edge.source == output}

    def descendants(self, node_ids: Iterable[str]) -> set[str]:
        result: set[str] = set()
        stack = list(node_ids)
        while stack:
            current = stack.pop()
            for child in self.successors(current):
                if child not in result:
                    result.add(child)
                    stack.append(child)
        return result

    def incoming_sources(self, node_id: str) -> list[str]:
        return [edge.source for edge in self.edges if edge.destination == node_id]

    def to_dict(self, include_version: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "input": self.input.to_dict(),
            "nodes": [self.nodes[node_id].to_dict() for node_id in self.topology],
            "edges": [edge.to_dict() for edge in sorted(self.edges, key=lambda e: (e.source, e.destination))],
            "outputs": list(self.outputs),
        }
        if include_version:
            result["graph_version"] = self.graph_version
        return result

    def fingerprint(self, include_version: bool = True) -> str:
        return sha256_json(self.to_dict(include_version=include_version))

    def clone(self) -> "Graph":
        return copy.deepcopy(self)


@dataclass
class GraphLoadResult:
    graph: Graph | None
    failures: list[Failure]
    fatal: bool
    node_parameter_failures: dict[str, list[Failure]] = field(default_factory=dict)
    source_hash: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "fatal": self.fatal,
            "source_hash": self.source_hash,
            "graph_version": self.graph.graph_version if self.graph else None,
            "graph_fingerprint": self.graph.fingerprint() if self.graph else None,
            "failures": [failure.to_dict() for failure in self.failures],
        }


def _failure(
    code: str,
    message: str,
    *,
    stage: str,
    node_id: str | None = None,
    exception_type: str | None = None,
    details: dict[str, Any] | None = None,
) -> Failure:
    return Failure(
        code=code,
        message=message,
        stage=stage,
        node_id=node_id,
        exception_type=exception_type,
        details=details or {},
    )


def _as_int(value: Any, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    return int(value)


def _as_pair(value: Any, label: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{label} must be a two-element array")
    return (_as_int(value[0], f"{label}[0]"), _as_int(value[1], f"{label}[1]"))


def _as_shape(value: Any, label: str, dimensions: int) -> tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != dimensions:
        raise ValueError(f"{label} must have {dimensions} dimensions")
    result = tuple(_as_int(item, label) for item in value)
    if any(item <= 0 for item in result):
        raise ValueError(f"{label} values must be positive")
    return result


def _parse_node(
    raw: dict[str, Any],
    index: int,
    base_dir: Path,
    failures: list[Failure],
) -> QuantNode:
    node_id = raw.get("id")
    label = f"nodes[{index}]"
    if not isinstance(node_id, str) or not node_id:
        raise ValueError(f"{label}.id must be a non-empty string")
    label = f"node {node_id}"

    def bad(message: str, code: str = STRUCTURAL_CODE) -> None:
        failures.append(
            _failure(code, message, stage="graph_load", node_id=node_id, exception_type="GraphValidationError")
        )

    try:
        input_tensor = raw.get("input", raw.get("input_tensor"))
        output_tensor = raw.get("output", raw.get("output_tensor", node_id))
        if not isinstance(input_tensor, str) or not input_tensor:
            raise ValueError(f"{label}: input must be a non-empty tensor name")
        if not isinstance(output_tensor, str) or not output_tensor:
            raise ValueError(f"{label}: output must be a non-empty tensor name")
        in_channels = _as_int(raw.get("in_channels"), f"{label}.in_channels")
        out_channels = _as_int(raw.get("out_channels"), f"{label}.out_channels")
        kernel = _as_pair(raw.get("kernel"), f"{label}.kernel")
        stride = _as_pair(raw.get("stride"), f"{label}.stride")
        padding = _as_pair(raw.get("padding"), f"{label}.padding")
        weight_pack = raw.get("weight_pack")
        if not isinstance(weight_pack, str) or not weight_pack:
            raise ValueError(f"{label}: weight_pack must be a non-empty key/path")
        weight_shape_raw = raw.get("weight_shape", [out_channels, in_channels, kernel[0], kernel[1]])
        weight_shape = _as_shape(weight_shape_raw, f"{label}.weight_shape", 4)
        weight_bits = _as_int(raw.get("weight_bit_width"), f"{label}.weight_bit_width")
        weight_scale = float(raw.get("weight_scale"))
        activation_bits = _as_int(
            raw.get("activation_bit_width"), f"{label}.activation_bit_width"
        )
        activation_scale = float(raw.get("activation_scale"))
        accumulator_bits = _as_int(raw.get("accumulator_bits", 32), f"{label}.accumulator_bits")
        overflow_policy = str(raw.get("overflow_policy", "error"))
    except (TypeError, ValueError) as exc:
        bad(str(exc))
        # A placeholder keeps graph-level structural diagnostics available.  It
        # is marked by the accompanying parameter failure.
        return QuantNode(
            id=node_id,
            input_tensor=str(raw.get("input", raw.get("input_tensor", ""))),
            output_tensor=str(raw.get("output", raw.get("output_tensor", node_id))),
            in_channels=0,
            out_channels=0,
            kernel=(1, 1),
            stride=(1, 1),
            padding=(0, 0),
            weight_pack=str(raw.get("weight_pack", "")),
            weight_shape=(0, 0, 0, 0),
            weight_bit_width=0,
            weight_scale=0.0,
            activation_bit_width=0,
            activation_scale=0.0,
            accumulator_bits=32,
            overflow_policy="error",
        )

    if in_channels <= 0:
        bad(f"{label}: in_channels must be positive")
    if out_channels <= 0:
        bad(f"{label}: out_channels must be positive")
    if kernel[0] <= 0 or kernel[1] <= 0:
        bad(f"{label}: kernel dimensions must be positive")
    if stride[0] <= 0 or stride[1] <= 0:
        bad(f"{label}: stride dimensions must be positive")
    if padding[0] < 0 or padding[1] < 0:
        bad(f"{label}: padding must be non-negative")
    if weight_shape != (out_channels, in_channels, kernel[0], kernel[1]):
        bad(
            f"{label}: weight_shape {list(weight_shape)} does not match "
            f"[out_channels, in_channels, kernel_h, kernel_w]"
        )
    for name, bits in (("weight_bit_width", weight_bits), ("activation_bit_width", activation_bits)):
        if bits < 2 or bits > 16:
            failures.append(
                _failure(
                    QUANT_CODE,
                    f"{label}: {name} must be in [2, 16], got {bits}",
                    stage="graph_load",
                    node_id=node_id,
                    exception_type="QuantParamValidationError",
                )
            )
    for name, scale in (("weight_scale", weight_scale), ("activation_scale", activation_scale)):
        if not finite_positive(scale):
            failures.append(
                _failure(
                    QUANT_CODE,
                    f"{label}: {name} must be finite and positive, got {scale!r}",
                    stage="graph_load",
                    node_id=node_id,
                    exception_type="QuantParamValidationError",
                )
            )
    if accumulator_bits < 8 or accumulator_bits > 64:
        failures.append(
            _failure(
                QUANT_CODE,
                f"{label}: accumulator_bits must be in [8, 64], got {accumulator_bits}",
                stage="graph_load",
                node_id=node_id,
                exception_type="QuantParamValidationError",
            )
        )
    if overflow_policy not in {"error", "saturate"}:
        failures.append(
            _failure(
                QUANT_CODE,
                f"{label}: overflow_policy must be error or saturate",
                stage="graph_load",
                node_id=node_id,
                exception_type="QuantParamValidationError",
            )
        )
    return QuantNode(
        id=node_id,
        input_tensor=input_tensor,
        output_tensor=output_tensor,
        in_channels=in_channels,
        out_channels=out_channels,
        kernel=kernel,
        stride=stride,
        padding=padding,
        weight_pack=weight_pack,
        weight_shape=weight_shape,
        weight_bit_width=weight_bits,
        weight_scale=weight_scale,
        activation_bit_width=activation_bits,
        activation_scale=activation_scale,
        accumulator_bits=accumulator_bits,
        overflow_policy=overflow_policy,
    )


def load_graph(path: str | Path) -> GraphLoadResult:
    source = Path(path)
    failures: list[Failure] = []
    try:
        raw_bytes = source.read_bytes()
        source_hash = sha256_bytes(raw_bytes)
        data = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        failures.append(
            _failure(
                STRUCTURAL_CODE,
                f"cannot load graph JSON: {exc}",
                stage="graph_load",
                exception_type=type(exc).__name__,
            )
        )
        return GraphLoadResult(graph=None, failures=failures, fatal=True)
    if not isinstance(data, dict):
        failures.append(_failure(STRUCTURAL_CODE, "graph root must be an object", stage="graph_load"))
        return GraphLoadResult(graph=None, failures=failures, fatal=True)

    raw_input = data.get("input")
    if not isinstance(raw_input, dict):
        failures.append(_failure(STRUCTURAL_CODE, "graph input must be an object", stage="graph_load"))
        return GraphLoadResult(graph=None, failures=failures, fatal=True)
    try:
        input_name = raw_input.get("name")
        if not isinstance(input_name, str) or not input_name:
            raise ValueError("input.name must be a non-empty string")
        input_shape = _as_shape(raw_input.get("shape"), "input.shape", 4)
        input_pack = raw_input.get("pack", "input")
        if not isinstance(input_pack, str) or not input_pack:
            raise ValueError("input.pack must be a non-empty manifest key/path")
        input_bits = _as_int(raw_input.get("bit_width"), "input.bit_width")
        input_scale = float(raw_input.get("scale"))
        input_dtype = str(raw_input.get("dtype", "int8"))
        graph_input = GraphInput(
            name=input_name,
            shape=input_shape,
            pack=input_pack,
            bit_width=input_bits,
            scale=input_scale,
            dtype=input_dtype,
        )
    except (TypeError, ValueError) as exc:
        failures.append(
            _failure(STRUCTURAL_CODE, str(exc), stage="graph_load", exception_type="GraphValidationError")
        )
        return GraphLoadResult(graph=None, failures=failures, fatal=True)

    if input_bits < 2 or input_bits > 16:
        failures.append(
            _failure(
                QUANT_CODE,
                f"input.bit_width must be in [2, 16], got {input_bits}",
                stage="graph_load",
                exception_type="QuantParamValidationError",
            )
        )
    if not finite_positive(input_scale):
        failures.append(
            _failure(
                QUANT_CODE,
                f"input.scale must be finite and positive, got {input_scale!r}",
                stage="graph_load",
                exception_type="QuantParamValidationError",
            )
        )

    raw_nodes = data.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        failures.append(_failure(STRUCTURAL_CODE, "graph must contain a non-empty nodes array", stage="graph_load"))
        return GraphLoadResult(graph=None, failures=failures, fatal=True)
    nodes: dict[str, QuantNode] = {}
    duplicate_ids: set[str] = set()
    for index, raw_node in enumerate(raw_nodes):
        if not isinstance(raw_node, dict):
            failures.append(_failure(STRUCTURAL_CODE, f"nodes[{index}] must be an object", stage="graph_load"))
            continue
        try:
            node = _parse_node(raw_node, index, source.parent, failures)
        except ValueError as exc:
            failures.append(_failure(STRUCTURAL_CODE, str(exc), stage="graph_load"))
            continue
        if node.id in nodes:
            duplicate_ids.add(node.id)
            failures.append(
                _failure(STRUCTURAL_CODE, f"duplicate node id: {node.id}", stage="graph_load", node_id=node.id)
            )
            continue
        nodes[node.id] = node
    if duplicate_ids:
        return GraphLoadResult(graph=None, failures=failures, fatal=True, source_hash=source_hash)

    raw_edges = data.get("edges")
    if not isinstance(raw_edges, list):
        failures.append(_failure(STRUCTURAL_CODE, "edges must be an array", stage="graph_load"))
        raw_edges = []
    edges: list[Edge] = []
    seen_edges: set[tuple[str, str]] = set()
    for index, raw_edge in enumerate(raw_edges):
        if not isinstance(raw_edge, dict):
            failures.append(_failure(STRUCTURAL_CODE, f"edges[{index}] must be an object", stage="graph_load"))
            continue
        edge_source = raw_edge.get("from")
        edge_destination = raw_edge.get("to")
        if not isinstance(edge_source, str) or not isinstance(edge_destination, str):
            failures.append(
                _failure(STRUCTURAL_CODE, f"edges[{index}] requires string from/to", stage="graph_load")
            )
            continue
        key = (edge_source, edge_destination)
        if key in seen_edges:
            failures.append(
                _failure(STRUCTURAL_CODE, f"duplicate edge: {edge_source} -> {edge_destination}", stage="graph_load")
            )
            continue
        seen_edges.add(key)
        edges.append(Edge(edge_source, edge_destination))

    raw_outputs = data.get("outputs", [])
    if not isinstance(raw_outputs, list) or any(not isinstance(item, str) for item in raw_outputs):
        failures.append(_failure(STRUCTURAL_CODE, "outputs must be an array of tensor names", stage="graph_load"))
        outputs: list[str] = []
    else:
        outputs = list(raw_outputs)

    for edge in edges:
        if edge.destination not in nodes:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"edge target node does not exist: {edge.destination}",
                    stage="graph_load",
                    node_id=edge.destination,
                )
            )

    producer: dict[str, str | None] = {graph_input.name: None}
    for node in nodes.values():
        if node.output_tensor in producer:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"tensor has multiple producers: {node.output_tensor}",
                    stage="graph_load",
                    node_id=node.id,
                )
            )
        else:
            producer[node.output_tensor] = node.id
    for edge in edges:
        if edge.source not in producer:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"edge source tensor does not exist: {edge.source}",
                    stage="graph_load",
                    node_id=edge.destination,
                )
            )
    for node in nodes.values():
        incoming = [edge.source for edge in edges if edge.destination == node.id]
        if len(incoming) != 1:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"node {node.id} must have exactly one incoming edge, got {len(incoming)}",
                    stage="graph_load",
                    node_id=node.id,
                )
            )
        elif incoming[0] != node.input_tensor:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"node {node.id} input {node.input_tensor!r} does not match edge source {incoming[0]!r}",
                    stage="graph_load",
                    node_id=node.id,
                )
            )
    for output in outputs:
        if output not in producer:
            failures.append(
                _failure(STRUCTURAL_CODE, f"graph output does not exist: {output}", stage="graph_load")
            )

    if not edges and nodes:
        failures.append(_failure(STRUCTURAL_CODE, "graph nodes have no incoming edges", stage="graph_load"))

    graph = Graph(
        input=graph_input,
        nodes=nodes,
        edges=tuple(edges),
        outputs=outputs,
        graph_version=int(data.get("graph_version", 1)),
        schema_version=int(data.get("schema_version", 1)),
    )
    graph.refresh_topology()

    # Cycle detection.  A valid topology has one entry per node.
    if len(graph.topology) != len(nodes):
        failures.append(
            _failure(STRUCTURAL_CODE, "operator graph contains a cycle", stage="graph_load")
        )

    structural_failures = [failure for failure in failures if failure.code == STRUCTURAL_CODE]
    if structural_failures:
        return GraphLoadResult(
            graph=graph,
            failures=failures,
            fatal=True,
            source_hash=source_hash,
        )

    shapes: dict[str, tuple[int, int, int, int]] = {graph_input.name: graph_input.shape}
    for node_id in graph.topology:
        node = nodes[node_id]
        if node.input_tensor not in shapes:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"node {node_id} input tensor has no upstream shape: {node.input_tensor}",
                    stage="graph_load",
                    node_id=node_id,
                )
            )
            continue
        batch, channels, height, width = shapes[node.input_tensor]
        if channels != node.in_channels:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"node {node_id} expects {node.in_channels} input channels but upstream provides {channels}",
                    stage="graph_load",
                    node_id=node_id,
                )
            )
        out_h = (height + 2 * node.padding[0] - node.kernel[0]) // node.stride[0] + 1
        out_w = (width + 2 * node.padding[1] - node.kernel[1]) // node.stride[1] + 1
        if out_h <= 0 or out_w <= 0:
            failures.append(
                _failure(
                    STRUCTURAL_CODE,
                    f"node {node_id} produces a non-positive output shape {out_h}x{out_w}",
                    stage="graph_load",
                    node_id=node_id,
                )
            )
            continue
        shapes[node.output_tensor] = (batch, node.out_channels, out_h, out_w)
    graph.output_shapes = shapes

    node_parameter_failures: dict[str, list[Failure]] = {}
    for failure in failures:
        if failure.code == QUANT_CODE and failure.node_id:
            node_parameter_failures.setdefault(failure.node_id, []).append(failure)
    if any(failure.code == QUANT_CODE and failure.node_id is None for failure in failures):
        structural_failures.append(
            _failure(
                STRUCTURAL_CODE,
                "graph input quantization parameters are invalid",
                stage="graph_load",
            )
        )
        graph = graph
        return GraphLoadResult(
            graph=graph,
            failures=failures,
            fatal=True,
            node_parameter_failures=node_parameter_failures,
            source_hash=source_hash,
        )

    fatal = bool([failure for failure in failures if failure.code == STRUCTURAL_CODE])
    return GraphLoadResult(
        graph=graph,
        failures=failures,
        fatal=fatal,
        node_parameter_failures=node_parameter_failures,
        source_hash=source_hash,
    )


def normalized_graph_from_dict(data: dict[str, Any]) -> Graph:
    """Load the normalized graph format written by ``Graph.to_dict``."""

    raw_input = data["input"]
    graph_input = GraphInput(
        name=raw_input["name"],
        shape=tuple(int(v) for v in raw_input["shape"]),
        pack=raw_input["pack"],
        bit_width=int(raw_input["bit_width"]),
        scale=float(raw_input["scale"]),
        dtype=raw_input.get("dtype", "int8"),
    )
    nodes: dict[str, QuantNode] = {}
    for raw_node in data["nodes"]:
        node = QuantNode(
            id=raw_node["id"],
            input_tensor=raw_node["input"],
            output_tensor=raw_node["output"],
            in_channels=int(raw_node["in_channels"]),
            out_channels=int(raw_node["out_channels"]),
            kernel=tuple(int(v) for v in raw_node["kernel"]),
            stride=tuple(int(v) for v in raw_node["stride"]),
            padding=tuple(int(v) for v in raw_node["padding"]),
            weight_pack=raw_node["weight_pack"],
            weight_shape=tuple(int(v) for v in raw_node["weight_shape"]),
            weight_bit_width=int(raw_node["weight_bit_width"]),
            weight_scale=float(raw_node["weight_scale"]),
            activation_bit_width=int(raw_node["activation_bit_width"]),
            activation_scale=float(raw_node["activation_scale"]),
            accumulator_bits=int(raw_node.get("accumulator_bits", 32)),
            overflow_policy=raw_node.get("overflow_policy", "error"),
        )
        nodes[node.id] = node
    edges = tuple(Edge(item["from"], item["to"]) for item in data["edges"])
    graph = Graph(
        input=graph_input,
        nodes=nodes,
        edges=edges,
        outputs=list(data["outputs"]),
        graph_version=int(data.get("graph_version", 1)),
        schema_version=int(data.get("schema_version", 1)),
    )
    graph.refresh_topology()
    return graph


def canonical_graph_json(graph: Graph) -> str:
    return canonical_json(graph.to_dict())
