"""Artifact compilation and the independent full-compile numeric baseline."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .graph import SHAPE_SENSITIVE_OPS, plan_request_graph
from .keys import make_compile_key
from .tensors import Tensor, compare_outputs, run_graph
from .util import canonical_json, sha256_hex

ARTIFACT_SCHEMA = "flight-compiled-unit/v1"
ARTIFACT_ABI = "flight-kernel-abi/v1"


def compile_scope(node: Mapping[str, Any]) -> str:
    if node.get("op") in SHAPE_SENSITIVE_OPS:
        return "shape_specialized"
    return "shape_independent"


def _concrete_axes(axis: Any, rank: int) -> list[int]:
    raw = axis if isinstance(axis, (list, tuple)) else [axis]
    axes = sorted({int(item) % rank for item in raw})
    return axes


def unit_kernel_spec(
    node: Mapping[str, Any], plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Freeze all shape-derived kernel choices for one unit."""

    scope = compile_scope(node)
    if scope == "shape_independent":
        return None
    node_id = str(node["id"])
    op = node.get("op")
    attrs = dict(node.get("attrs", {}))
    input_shapes = [list(shape) for shape in plan["node_input_shapes"][node_id]]
    output_shape = list(plan["node_output_shapes"][node_id])
    spec: dict[str, Any] = {
        "input_shapes": input_shapes,
        "output_shape": output_shape,
    }
    if op == "reduce_sum":
        rank = len(input_shapes[0])
        raw_axis = attrs.get("axis", attrs.get("axes"))
        spec["axes"] = _concrete_axes(raw_axis, rank)
        spec["keepdims"] = bool(attrs.get("keepdims", False))
    elif op == "reshape":
        spec["output_shape"] = output_shape
    elif op == "transpose":
        spec["perm"] = list(attrs.get("perm") or list(reversed(range(len(input_shapes[0])))))
    elif op == "concat":
        spec["axis"] = int(attrs.get("axis", 0)) % len(output_shape)
    elif op == "slice":
        spec["axis"] = int(attrs.get("axis", 0)) % len(input_shapes[0])
        spec["start"] = int(attrs.get("start", 0))
        spec["stop"] = attrs.get("stop")
        spec["step"] = int(attrs.get("step", 1))
    elif op == "softmax":
        spec["axis"] = int(attrs.get("axis", -1)) % len(input_shapes[0])
    return spec


def unit_descriptor(
    model_id: str,
    node: Mapping[str, Any],
    plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Projected unit key material.

    A shape-specialized unit includes the concrete input/output shapes, whereas
    a shape-independent unit deliberately excludes them.  Neither variant
    includes the model version: unchanged subgraphs must keep the same artifact
    identifier across a version update, while compile *entries* remain fully
    versioned.
    """

    node_id = str(node["id"])
    scope = compile_scope(node)
    descriptor: dict[str, Any] = {
        "schema": ARTIFACT_SCHEMA,
        "abi": ARTIFACT_ABI,
        "model_id": model_id,
        "unit_id": node_id,
        "op": node.get("op"),
        "scope": scope,
        "structural_digest": plan["structural_digests"][node_id],
    }
    if scope == "shape_specialized":
        descriptor["input_shapes"] = [
            list(shape) for shape in plan["node_input_shapes"][node_id]
        ]
        descriptor["output_shape"] = list(plan["node_output_shapes"][node_id])
    return descriptor


def artifact_identity(model_id: str, node: Mapping[str, Any], plan: Mapping[str, Any]) -> tuple[str, str, dict[str, Any]]:
    descriptor = unit_descriptor(model_id, node, plan)
    unit_key = "uk_" + sha256_hex(descriptor, length=24)
    artifact_id = "art_" + sha256_hex(
        {"unit_key": unit_key, "unit_id": node["id"], "abi": ARTIFACT_ABI}, length=24
    )
    return unit_key, artifact_id, descriptor


def _shape_work_factor(output_shape: Sequence[int]) -> int:
    # A deterministic static-shape specialization cost.  Larger kernels get
    # more expensive to code-generate, while the full path pays this for every
    # operator on every request.
    return 1 + sum(int(dim) for dim in output_shape) // 16


def unit_compile_work(
    node: Mapping[str, Any], output_shape: Sequence[int], scope: str
) -> int:
    base = int(node.get("compile_cost", 1))
    if scope == "shape_specialized":
        return max(1, base * _shape_work_factor(output_shape))
    return max(1, base)


def compile_unit(
    model_id: str,
    node: Mapping[str, Any],
    plan: Mapping[str, Any],
    *,
    compile_key: str,
    ms_per_work_unit: float,
) -> dict[str, Any]:
    node_id = str(node["id"])
    scope = compile_scope(node)
    unit_key, artifact_id, descriptor = artifact_identity(model_id, node, plan)
    output_shape = list(plan["node_output_shapes"][node_id])
    work = unit_compile_work(node, output_shape, scope)
    return {
        "schema": ARTIFACT_SCHEMA,
        "artifact_id": artifact_id,
        "unit_key": unit_key,
        "identity_formula": "sha256({unit_key, unit_id, artifact_abi})",
        "unit_id": node_id,
        "model_id": model_id,
        "op": node.get("op"),
        "scope": scope,
        "structural_digest": descriptor["structural_digest"],
        "input_shapes": descriptor.get("input_shapes"),
        "output_shape": output_shape,
        "kernel_spec": unit_kernel_spec(node, plan),
        "compile_work": work,
        "compile_ms": round(work * ms_per_work_unit, 6),
        "origin_compile_key": compile_key,
        "origin_plan_fingerprint": plan_fingerprint(plan),
    }


def plan_fingerprint(plan: Mapping[str, Any]) -> str:
    payload = {
        "model": plan["model"]["model_id"],
        "version": plan["version"],
        "input_shapes": {name: list(shape) for name, shape in plan["input_shapes"].items()},
        "nodes": plan["topo_nodes"],
        "structural_digests": plan["structural_digests"],
    }
    return "plan_" + sha256_hex(payload, length=20)


def compile_entry_units(
    plan: Mapping[str, Any],
    compile_key: str,
    *,
    artifact_registry: dict[str, dict[str, Any]],
    ms_per_work_unit: float,
    compile_origin: str,
) -> dict[str, Any]:
    """Compile or reuse every unit for one cache entry."""

    model_id = str(plan["model"]["model_id"])
    artifacts: dict[str, dict[str, Any]] = {}
    compiled_units: list[str] = []
    reused_units: list[str] = []
    unit_reports: list[dict[str, Any]] = []
    compile_work = 0
    compile_ms = 0.0
    shape_specialized_kernel_compiles = 0
    for node_id in plan["topo_nodes"]:
        node = plan["nodes_by_id"][node_id]
        scope = compile_scope(node)
        unit_key, artifact_id, descriptor = artifact_identity(model_id, node, plan)
        existing = artifact_registry.get(artifact_id)
        if existing is not None:
            artifact = existing
            action = "reused"
            reused_units.append(node_id)
        else:
            artifact = compile_unit(
                model_id,
                node,
                plan,
                compile_key=compile_key,
                ms_per_work_unit=ms_per_work_unit,
            )
            artifact_registry[artifact_id] = artifact
            action = "compiled"
            compiled_units.append(node_id)
            compile_work += int(artifact["compile_work"])
            compile_ms += float(artifact["compile_ms"])
            if scope == "shape_specialized":
                shape_specialized_kernel_compiles += 1
        artifacts[node_id] = dict(artifact)
        unit_reports.append(
            {
                "unit_id": node_id,
                "op": node.get("op"),
                "scope": scope,
                "action": action,
                "unit_key": unit_key,
                "artifact_id": artifact_id,
                "compile_work": int(artifact["compile_work"]) if action == "compiled" else 0,
                "compile_ms": float(artifact["compile_ms"]) if action == "compiled" else 0.0,
                "reused_from_compile_key": artifact.get("origin_compile_key")
                if action == "reused"
                else None,
            }
        )
    return {
        "compile_key": compile_key,
        "compile_origin": compile_origin,
        "artifacts": artifacts,
        "compiled_units": compiled_units,
        "reused_units": reused_units,
        "unit_reports": unit_reports,
        "compile_work": compile_work,
        "compile_ms": round(compile_ms, 6),
        "shape_specialized_kernel_compiles": shape_specialized_kernel_compiles,
        "artifact_ids": {unit_id: artifact["artifact_id"] for unit_id, artifact in artifacts.items()},
    }


def full_compile_work(plan: Mapping[str, Any]) -> int:
    total = 0
    for node_id in plan["topo_nodes"]:
        node = plan["nodes_by_id"][node_id]
        total += unit_compile_work(
            node,
            plan["node_output_shapes"][node_id],
            compile_scope(node),
        )
    return total


@dataclass
class FullCompileResult:
    outputs: dict[str, Tensor]
    outputs_meta: dict[str, Any]
    compile_work: int
    compile_ms: float
    compile_count: int


class FullCompileBaseline:
    """Independent full compilation path.

    This path intentionally ignores the artifact cache.  Every request compiles
    and executes the complete graph, which makes it useful as the numeric oracle
    for specialized artifacts.
    """

    def __init__(self, project: Mapping[str, Any]) -> None:
        self.project = project
        self.compile_count = 0
        self.total_compile_work = 0
        self.total_compile_ms = 0.0
        config = project.get("cache_config", {})
        self.ms_per_work_unit = float(config.get("ms_per_work_unit", 0.0125))

    def compile_and_execute(
        self, plan: Mapping[str, Any], input_tensors: Mapping[str, Tensor]
    ) -> FullCompileResult:
        self.compile_count += 1
        work = full_compile_work(plan)
        compile_ms = round(work * self.ms_per_work_unit, 6)
        self.total_compile_work += work
        self.total_compile_ms += compile_ms
        nodes = [plan["nodes_by_id"][node_id] for node_id in plan["topo_nodes"]]
        outputs = run_graph(nodes, input_tensors, plan["outputs"], kernel_specs=None)
        return FullCompileResult(
            outputs=outputs,
            outputs_meta={name: tensor.to_dict() for name, tensor in outputs.items()},
            compile_work=work,
            compile_ms=compile_ms,
            compile_count=1,
        )


def execute_specialized(
    plan: Mapping[str, Any],
    artifacts: Mapping[str, Mapping[str, Any]],
    input_tensors: Mapping[str, Tensor],
) -> dict[str, Tensor]:
    kernel_specs = {
        unit_id: artifact.get("kernel_spec") for unit_id, artifact in artifacts.items()
    }
    nodes = [plan["nodes_by_id"][node_id] for node_id in plan["topo_nodes"]]
    return run_graph(nodes, input_tensors, plan["outputs"], kernel_specs=kernel_specs)


def compare_specialized_to_baseline(
    specialized: Mapping[str, Tensor],
    baseline: Mapping[str, Tensor],
    tolerance: float,
) -> dict[str, Any]:
    return compare_outputs(specialized, baseline, tolerance)


def make_request_plan(
    project: Mapping[str, Any],
    request: Mapping[str, Any],
    current_versions: Mapping[str, str],
) -> dict[str, Any]:
    return plan_request_graph(project, request, current_versions=current_versions)


def compile_key_for_plan(plan: Mapping[str, Any]) -> str:
    return make_compile_key(
        str(plan["model"]["model_id"]), str(plan["version"]), plan["input_shapes"]
    )
