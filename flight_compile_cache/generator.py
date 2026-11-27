"""Deterministic airline inference-workload fixture generator."""

from __future__ import annotations

import copy
import hashlib
import random
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .graph import (
    get_graph,
    graph_fingerprint,
    model_graph_fingerprints,
    nodes_by_id,
    shape_segment,
    version_changed_units,
)
from .keys import make_compile_key
from .util import atomic_write_json, canonical_json, sha256_hex

DEFAULT_SEED = 20261128
PROJECT_SCHEMA = "flight-inference-project/v1"
GENERATOR_VERSION = "1.0.0"


def _rng(master_seed: int, *material: Any) -> random.Random:
    digest = hashlib.sha256(
        canonical_json([master_seed, *material]).encode("utf-8")
    ).hexdigest()
    return random.Random(int(digest[:16], 16))


def _matrix(rng: random.Random, rows: int, cols: int, *, scale: float = 0.45) -> list[list[float]]:
    return [[round(rng.uniform(-scale, scale), 4) for _ in range(cols)] for _ in range(rows)]


def _vector(rng: random.Random, length: int, *, scale: float = 0.25) -> list[float]:
    return [round(rng.uniform(-scale, scale), 4) for _ in range(length)]


def _const_node(
    node_id: str,
    value: Any,
    *,
    compile_cost: int = 1,
    description: str = "静态参数",
) -> dict[str, Any]:
    shape, _ = _shape_and_flat(value)
    return {
        "id": node_id,
        "op": "const",
        "inputs": [],
        "out_shape": shape,
        "attrs": {"value": value},
        "compile_cost": compile_cost,
        "description": description,
    }


def _shape_and_flat(value: Any) -> tuple[list[int], list[float]]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [], [float(value)]
    if not isinstance(value, list) or not value:
        raise ValueError("constant must be a non-empty number or nested list")
    child_shape, child_flat = _shape_and_flat(value[0])
    flat = list(child_flat)
    for item in value[1:]:
        item_shape, item_flat = _shape_and_flat(item)
        if item_shape != child_shape:
            raise ValueError("constant must be rectangular")
        flat.extend(item_flat)
    return [len(value), *child_shape], flat


def _node(
    node_id: str,
    op: str,
    inputs: Sequence[str],
    out_shape: Sequence[Any],
    *,
    attrs: Mapping[str, Any] | None = None,
    compile_cost: int = 1,
    description: str = "",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "op": op,
        "inputs": list(inputs),
        "out_shape": list(out_shape),
        "attrs": dict(attrs or {}),
        "compile_cost": compile_cost,
        "description": description,
    }


def _delay_graph(seed: int, *, factor: float) -> dict[str, Any]:
    rng = _rng(seed, "delay", "parameters")
    wf = _matrix(rng, 12, 8)
    ww = _matrix(rng, 4, 8)
    bias = _vector(rng, 8)
    gain = _vector(rng, 8)
    graph = {
        "inputs": [
            {"name": "flight_features", "shape": ["B", 12], "description": "航班计划特征"},
            {"name": "weather", "shape": ["B", 4], "description": "实时气象特征"},
            {"name": "slot_mask", "shape": ["B", 8], "description": "保障时段掩码"},
        ],
        "nodes": [
            _const_node("delay_Wf", wf, compile_cost=2, description="航班特征投影权重"),
            _const_node("delay_Ww", ww, compile_cost=1, description="气象特征投影权重"),
            _const_node("delay_bias", bias, compile_cost=1, description="偏置静态参数"),
            _const_node("delay_gain", gain, compile_cost=1, description="偏置缩放静态参数"),
            _node(
                "delay_f_proj",
                "matmul",
                ["flight_features", "delay_Wf"],
                ["B", 8],
                compile_cost=6,
                description="按航班数 B 专门化的投影矩阵乘",
            ),
            _node(
                "delay_w_proj",
                "matmul",
                ["weather", "delay_Ww"],
                ["B", 8],
                compile_cost=5,
                description="按航班数 B 专门化的气象投影",
            ),
            _node(
                "delay_bias_scaled",
                "mul",
                ["delay_bias", "delay_gain"],
                [8],
                compile_cost=1,
                description="形状无关的静态偏置预处理",
            ),
            _node("delay_sum", "add", ["delay_f_proj", "delay_w_proj"], ["B", 8], compile_cost=1),
            _node("delay_bias_add", "add", ["delay_sum", "delay_bias_scaled"], ["B", 8], compile_cost=1),
            _node("delay_mask", "mul", ["delay_bias_add", "slot_mask"], ["B", 8], compile_cost=1),
            _node("delay_relu", "relu", ["delay_mask"], ["B", 8], compile_cost=1),
            _node(
                "delay_scores",
                "reduce_sum",
                ["delay_relu"],
                ["B"],
                attrs={"axis": 1, "keepdims": False},
                compile_cost=4,
                description="按航班维归约，依赖 B 的静态形状",
            ),
            _node(
                "delay_mean",
                "scale",
                ["delay_scores"],
                ["B"],
                attrs={"factor": factor},
                compile_cost=1,
                description="延误建议输出缩放",
            ),
        ],
        "outputs": ["delay_mean"],
    }
    return graph


def _resource_graph(seed: int, *, task_factor: float) -> dict[str, Any]:
    rng = _rng(seed, "resource", "parameters")
    wt = _matrix(rng, 10, 6)
    ws = _matrix(rng, 6, 6)
    bias = _vector(rng, 6)
    gain = _vector(rng, 6)
    graph = {
        "inputs": [
            {"name": "ground_tasks", "shape": ["T", 10], "description": "地面保障任务特征"},
            {"name": "flight_slots", "shape": ["B", 6], "description": "航班可用保障时段"},
        ],
        "nodes": [
            _const_node("resource_Wt", wt, compile_cost=2, description="保障任务投影权重"),
            _const_node("resource_Ws", ws, compile_cost=2, description="航班时段投影权重"),
            _const_node("resource_bias", bias, compile_cost=1, description="资源分配偏置"),
            _const_node("resource_gain", gain, compile_cost=1, description="资源分配偏置缩放"),
            _node(
                "resource_task_proj",
                "matmul",
                ["ground_tasks", "resource_Wt"],
                ["T", 6],
                compile_cost=7,
                description="按保障任务数 T 专门化的投影",
            ),
            _node(
                "resource_slot_proj",
                "matmul",
                ["flight_slots", "resource_Ws"],
                ["B", 6],
                compile_cost=6,
                description="按航班数 B 专门化的时段投影",
            ),
            _node(
                "resource_bias_scaled",
                "mul",
                ["resource_bias", "resource_gain"],
                [6],
                compile_cost=1,
                description="形状无关的静态偏置预处理",
            ),
            _node(
                "resource_task_shift",
                "add",
                ["resource_task_proj", "resource_bias_scaled"],
                ["T", 6],
                compile_cost=1,
            ),
            _node(
                "resource_slot_shift",
                "add",
                ["resource_slot_proj", "resource_bias_scaled"],
                ["B", 6],
                compile_cost=1,
            ),
            _node(
                "resource_task_energy",
                "reduce_sum",
                ["resource_task_shift"],
                ["T"],
                attrs={"axis": 1, "keepdims": False},
                compile_cost=4,
                description="按 T 归约的任务能量",
            ),
            _node(
                "resource_slot_energy",
                "reduce_sum",
                ["resource_slot_shift"],
                ["B"],
                attrs={"axis": 1, "keepdims": False},
                compile_cost=4,
                description="按 B 归约的时段能量",
            ),
            _node(
                "resource_task_norm",
                "scale",
                ["resource_task_energy"],
                ["T"],
                attrs={"factor": task_factor},
                compile_cost=1,
                description="保障任务建议输出缩放",
            ),
            _node(
                "resource_slot_norm",
                "scale",
                ["resource_slot_energy"],
                ["B"],
                attrs={"factor": 0.30},
                compile_cost=1,
                description="航班资源建议输出缩放",
            ),
        ],
        "outputs": ["resource_task_norm", "resource_slot_norm"],
    }
    return graph


def _model(
    model_id: str,
    display_name: str,
    version_graphs: Mapping[str, tuple[int, dict[str, Any]]],
    *,
    initial_version: str,
    available_since: Mapping[str, float],
) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    manifest = []
    for version, (revision, graph) in version_graphs.items():
        versions[version] = {
            "revision": revision,
            "graph": graph,
        }
        manifest.append(
            {
                "version": version,
                "revision": revision,
                "graph_fingerprint": graph_fingerprint(graph),
                "available_since": available_since.get(version, 0),
                "changed_units": [],
                "change_summary": "初始版本" if version == initial_version else "增量版本",
            }
        )
    return {
        "model_id": model_id,
        "display_name": display_name,
        "initial_version": initial_version,
        "versions": versions,
        "version_manifest": manifest,
    }


def _request(
    request_id: str,
    time: float,
    model_id: str,
    version: str,
    input_shapes: Mapping[str, Sequence[int]],
    *,
    data_seed: int,
    expectation: Mapping[str, Any],
    note: str = "",
) -> dict[str, Any]:
    return {
        "id": request_id,
        "time": time,
        "model_id": model_id,
        "model_version": version,
        "input_shapes": {
            name: [int(dim) for dim in shape] for name, shape in sorted(input_shapes.items())
        },
        "data_seed": data_seed,
        "expectation": dict(expectation),
        "note": note,
    }


def _delay_shapes(batch: int) -> dict[str, list[int]]:
    return {
        "flight_features": [batch, 12],
        "weather": [batch, 4],
        "slot_mask": [batch, 8],
    }


def _resource_shapes(tasks: int, batch: int) -> dict[str, list[int]]:
    return {
        "ground_tasks": [tasks, 10],
        "flight_slots": [batch, 6],
    }


def generate_project(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    delay_v1 = _delay_graph(seed, factor=0.55)
    delay_v2 = copy.deepcopy(delay_v1)
    for node in delay_v2["nodes"]:
        if node["id"] == "delay_mean":
            node["attrs"]["factor"] = 0.67
    resource_v1 = _resource_graph(seed, task_factor=0.40)
    resource_v2 = copy.deepcopy(resource_v1)
    for node in resource_v2["nodes"]:
        if node["id"] == "resource_task_norm":
            node["attrs"]["factor"] = 0.46

    models = {
        "delay_predict": _model(
            "delay_predict",
            "航班延误预测",
            {"v1": (1, delay_v1), "v2": (2, delay_v2)},
            initial_version="v1",
            available_since={"v1": 0, "v2": 120},
        ),
        "resource_alloc": _model(
            "resource_alloc",
            "地面保障资源分配",
            {"v1": (1, resource_v1), "v2": (2, resource_v2)},
            initial_version="v1",
            available_since={"v1": 0, "v2": 260},
        ),
    }

    requests: list[dict[str, Any]] = []
    seed_counter = seed * 100

    def add(
        time: float,
        model_id: str,
        version: str,
        shapes: Mapping[str, Sequence[int]],
        *,
        note: str = "",
    ) -> None:
        nonlocal seed_counter
        seed_counter += 1
        request_id = f"req-{len(requests) + 1:03d}"
        requests.append(
            _request(
                request_id,
                time,
                model_id,
                version,
                shapes,
                data_seed=seed_counter,
                expectation={"valid": True, "error_code": None},
                note=note,
            )
        )

    def add_invalid(
        time: float,
        model_id: str,
        version: str,
        shapes: Mapping[str, Sequence[int]],
        code: str,
        *,
        note: str,
    ) -> None:
        nonlocal seed_counter
        seed_counter += 1
        request_id = f"req-{len(requests) + 1:03d}"
        requests.append(
            _request(
                request_id,
                time,
                model_id,
                version,
                shapes,
                data_seed=seed_counter,
                expectation={"valid": False, "error_code": code},
                note=note,
            )
        )

    # Pre-update phase: repeated hits, shape changes, and enough distinct keys
    # to exceed the small LRU capacity.
    add(10, "delay_predict", "v1", _delay_shapes(3), note="首次登记 B=3")
    add(18, "delay_predict", "v1", _delay_shapes(3), note="同版本同形状重放")
    add(26, "resource_alloc", "v1", _resource_shapes(2, 3), note="资源模型首次登记")
    add(34, "delay_predict", "v1", _delay_shapes(4), note="航班数变化 B=4")
    add(42, "resource_alloc", "v1", _resource_shapes(2, 3), note="资源模型重复形状")
    add(50, "delay_predict", "v1", _delay_shapes(3), note="B=3 再次出现")
    add(58, "resource_alloc", "v1", _resource_shapes(3, 3), note="保障任务数 T=3")
    add(66, "delay_predict", "v1", _delay_shapes(5), note="航班数变化 B=5")
    add(74, "resource_alloc", "v1", _resource_shapes(2, 3), note="资源模型重复形状")
    add(82, "delay_predict", "v1", _delay_shapes(4), note="B=4 再次出现")
    add(90, "resource_alloc", "v1", _resource_shapes(4, 4), note="B/T 同时变化")
    add(98, "delay_predict", "v1", _delay_shapes(3), note="B=3 再次出现")
    add(106, "resource_alloc", "v1", _resource_shapes(2, 3), note="资源模型重复形状")

    add_invalid(
        108,
        "delay_predict",
        "v1",
        {"flight_features": [3, 12], "weather": [3, 4]},
        "E_MISSING_INPUT",
        note="缺少 slot_mask 输入，应跳过",
    )
    add_invalid(
        114,
        "resource_alloc",
        "v1",
        {"ground_tasks": [0, 10], "flight_slots": [3, 6]},
        "E_INVALID_DIMENSION",
        note="保障任务维为 0，应跳过",
    )

    # Delay model update at t=120.  Only delay_mean changes; all upstream units
    # are reusable and must keep their artifact identifiers.
    add(126, "delay_predict", "v2", _delay_shapes(3), note="更新后同形状")
    add(134, "delay_predict", "v2", _delay_shapes(4), note="更新后 B=4")
    add(142, "resource_alloc", "v1", _resource_shapes(3, 3), note="其他模型产物保留")
    add(150, "delay_predict", "v2", _delay_shapes(5), note="更新后新形状 B=5")
    add(158, "resource_alloc", "v1", _resource_shapes(4, 4), note="其他模型重复形状")
    add(166, "delay_predict", "v2", _delay_shapes(6), note="更新后新形状 B=6")
    add(174, "resource_alloc", "v1", _resource_shapes(2, 3), note="其他模型重复形状")
    add(182, "delay_predict", "v2", _delay_shapes(3), note="更新后命中")

    add_invalid(
        168,
        "delay_predict",
        "v2",
        {"flight_features": [3, 11], "weather": [3, 4], "slot_mask": [3, 8]},
        "E_INPUT_SHAPE_MISMATCH",
        note="静态特征维 12 被写成 11，应跳过",
    )
    add_invalid(
        186,
        "resource_alloc",
        "v1",
        {"ground_tasks": [2, 10], "flight_slots": [3, 6], "extra_weather": [3, 1]},
        "E_UNKNOWN_INPUT",
        note="未知输入张量，应跳过",
    )
    add(190, "resource_alloc", "v1", _resource_shapes(5, 4), note="保障任务数突变 T=5")

    # Resource model update at t=260.
    add(266, "resource_alloc", "v2", _resource_shapes(2, 3), note="资源模型更新后同形状")
    add(274, "delay_predict", "v2", _delay_shapes(4), note="延误模型产物保留")
    add(282, "resource_alloc", "v2", _resource_shapes(3, 3), note="资源模型更新后 T=3")
    add(290, "resource_alloc", "v2", _resource_shapes(2, 3), note="资源模型更新后命中")
    add(298, "delay_predict", "v2", _delay_shapes(3), note="延误模型命中")
    add(306, "resource_alloc", "v2", _resource_shapes(4, 4), note="资源模型更新后 B/T 变化")

    add_invalid(
        288,
        "delay_predict",
        "v1",
        _delay_shapes(3),
        "E_STALE_VERSION",
        note="版本更新后仍请求 v1，应跳过",
    )
    add_invalid(
        294,
        "unknown_model",
        "v1",
        {"x": [1, 1]},
        "E_MODEL_NOT_FOUND",
        note="未知模型，应跳过",
    )

    add(314, "delay_predict", "v2", _delay_shapes(6), note="延误模型更新后 B=6")
    add(322, "resource_alloc", "v2", _resource_shapes(2, 3), note="资源模型重复形状")
    add(330, "delay_predict", "v2", _delay_shapes(5), note="延误模型 B=5")
    add(338, "resource_alloc", "v2", _resource_shapes(3, 3), note="资源模型 T=3")
    add(346, "delay_predict", "v2", _delay_shapes(3), note="延误模型 B=3")
    add(354, "resource_alloc", "v2", _resource_shapes(4, 4), note="资源模型 B/T 变化")

    add_invalid(
        358,
        "delay_predict",
        "v2",
        {"flight_features": [-1, 12], "weather": [3, 4], "slot_mask": [3, 8]},
        "E_INVALID_DIMENSION",
        note="航班维为负，应跳过",
    )

    add(362, "delay_predict", "v2", _delay_shapes(4), note="延误模型 B=4")
    add(370, "resource_alloc", "v2", _resource_shapes(2, 3), note="资源模型重复形状")
    add(378, "delay_predict", "v2", _delay_shapes(8), note="形状突变 B=8")
    add(386, "resource_alloc", "v2", _resource_shapes(6, 6), note="形状突变 T=6,B=6")
    add(394, "delay_predict", "v2", _delay_shapes(8), note="B=8 重放")
    add(402, "resource_alloc", "v2", _resource_shapes(6, 6), note="T=6,B=6 重放")
    add(410, "delay_predict", "v2", _delay_shapes(3), note="B=3 重放")
    add(418, "resource_alloc", "v2", _resource_shapes(2, 3), note="资源模型重复形状")

    requests.sort(key=lambda item: (item["time"], item["id"]))

    events = [
        {
            "id": "evt-001",
            "time": 120,
            "type": "model_update",
            "model_id": "delay_predict",
            "from_version": "v1",
            "to_version": "v2",
            "description": "延误预测模型权重后处理版本更新",
        },
        {
            "id": "evt-002",
            "time": 260,
            "type": "model_update",
            "model_id": "resource_alloc",
            "from_version": "v1",
            "to_version": "v2",
            "description": "地面保障资源分配模型输出缩放更新",
        },
    ]

    project = {
        "schema_version": PROJECT_SCHEMA,
        "project_id": f"flight-scheduling-{seed}",
        "generator": {
            "name": "deterministic-flight-inference-generator",
            "version": GENERATOR_VERSION,
            "seed": seed,
        },
        "cache_config": {
            "capacity": 4,
            "policy": "LRU",
            "numeric_tolerance": 1e-6,
            "ms_per_work_unit": 0.0125,
        },
        "models": models,
        "requests": requests,
        "events": events,
        "expectations": {
            "request_expectations": {
                item["id"]: dict(item["expectation"]) for item in requests
            },
            "oracle_source": "deterministic generator fixed-seed metadata",
        },
    }

    # Fill version manifests with graph-diff derived scopes after the project
    # object exists.
    for model_id, model in models.items():
        for entry in model["version_manifest"]:
            if entry["version"] == model["initial_version"]:
                continue
            diff = version_changed_units(project, model_id, model["initial_version"], entry["version"])
            entry["changed_units"] = diff["affected_units"]
            entry["unaffected_units"] = diff["unaffected_units"]

    _attach_expectation_summary(project)
    return project


def _attach_expectation_summary(project: dict[str, Any]) -> None:
    groups: dict[str, dict[str, Any]] = {}
    segment_counts: dict[str, dict[str, int]] = {}
    valid = 0
    invalid = 0
    for request in project["requests"]:
        expectation = request["expectation"]
        if not expectation["valid"]:
            invalid += 1
            continue
        valid += 1
        model = project["models"][request["model_id"]]
        graph = get_graph(model, request["model_version"])
        segment = shape_segment(graph, request["input_shapes"])
        key = make_compile_key(request["model_id"], request["model_version"], request["input_shapes"])
        group_key = f"{request['model_id']}@{request['model_version']}"
        bucket = groups.setdefault(
            group_key,
            {
                "model_id": request["model_id"],
                "model_version": request["model_version"],
                "distinct_compile_keys": 0,
                "requests": 0,
                "shape_segments": {},
            },
        )
        bucket["requests"] += 1
        bucket["shape_segments"].setdefault(segment, 0)
        bucket["shape_segments"][segment] += 1
        key_set = bucket.setdefault("_keys", {})
        key_set[key] = True
        dist_key = f"{request['model_id']}@{request['model_version']}#{segment}"
        segment_counts.setdefault(dist_key, {})
        segment_counts[dist_key].setdefault(key, 0)
        segment_counts[dist_key][key] += 1
    for bucket in groups.values():
        bucket["distinct_compile_keys"] = len(bucket.pop("_keys", {}))
    distribution = []
    for group_key in sorted(segment_counts):
        model_version, segment = group_key.split("#", 1)
        model_id, version = model_version.rsplit("@", 1)
        distribution.append(
            {
                "model_id": model_id,
                "model_version": version,
                "shape_segment": segment,
                "distinct_keys": len(segment_counts[group_key]),
                "request_count": sum(segment_counts[group_key].values()),
            }
        )
    project["expectations"].update(
        {
            "valid_request_count": valid,
            "invalid_request_count": invalid,
            "distinct_compile_key_count": sum(
                item["distinct_compile_keys"] for item in groups.values()
            ),
            "by_model_version": {key: groups[key] for key in sorted(groups)},
            "shape_segment_distribution": distribution,
            "notes": [
                "A valid request with the same model version and normalized input shapes has one compile key.",
                "Shape-sensitive units are specialized per concrete request shape.",
                "Shape-independent units are reused across shapes and unchanged model versions.",
            ],
        }
    )


def _single_version_model(model: Mapping[str, Any], version: str = "v1") -> dict[str, Any]:
    copied = copy.deepcopy(model)
    copied["initial_version"] = version
    copied["versions"] = {version: copied["versions"][version]}
    manifest = [entry for entry in copied["version_manifest"] if entry["version"] == version]
    copied["version_manifest"] = manifest
    return copied


def _refresh_manifest_fingerprints(model: dict[str, Any]) -> None:
    for entry in model["version_manifest"]:
        version = entry["version"]
        if version in model["versions"]:
            entry["graph_fingerprint"] = graph_fingerprint(
                model["versions"][version]["graph"]
            )


def generate_edge_project(seed: int = DEFAULT_SEED) -> dict[str, Any]:
    base = generate_project(seed)
    valid = _single_version_model(base["models"]["delay_predict"])
    valid["model_id"] = "edge_valid_delay"
    valid["display_name"] = "边界样本：有效模型"

    broken_ref = _single_version_model(base["models"]["delay_predict"])
    broken_ref["model_id"] = "edge_broken_ref"
    broken_ref["display_name"] = "边界样本：缺失图引用"
    broken_ref["versions"]["v1"]["graph"]["nodes"].append(
        _node(
            "edge_broken_reader",
            "identity",
            ["tensor_that_does_not_exist"],
            ["B", 8],
            compile_cost=1,
            description="故意引用不存在的张量",
        )
    )
    _refresh_manifest_fingerprints(broken_ref)

    nonpositive = _single_version_model(base["models"]["delay_predict"])
    nonpositive["model_id"] = "edge_nonpositive_dim"
    nonpositive["display_name"] = "边界样本：非正维度"
    for item in nonpositive["versions"]["v1"]["graph"]["inputs"]:
        if item["name"] == "slot_mask":
            item["shape"] = ["B", 0]
    _refresh_manifest_fingerprints(nonpositive)

    not_closed = _single_version_model(base["models"]["delay_predict"])
    not_closed["model_id"] = "edge_not_closed"
    not_closed["display_name"] = "边界样本：算子张量不闭合"
    for node in not_closed["versions"]["v1"]["graph"]["nodes"]:
        if node["id"] == "delay_f_proj":
            node["out_shape"] = ["B", 7]
    _refresh_manifest_fingerprints(not_closed)

    models = {
        "edge_valid_delay": valid,
        "edge_broken_ref": broken_ref,
        "edge_nonpositive_dim": nonpositive,
        "edge_not_closed": not_closed,
    }
    requests = [
        _request(
            "edge-req-001",
            1,
            "edge_valid_delay",
            "v1",
            _delay_shapes(3),
            data_seed=seed * 1000 + 1,
            expectation={"valid": True, "error_code": None},
            note="边界工程中仍可正常推理的请求",
        ),
        _request(
            "edge-req-002",
            2,
            "edge_valid_delay",
            "v1",
            {"flight_features": [3, 12], "weather": [3, 4]},
            data_seed=seed * 1000 + 2,
            expectation={"valid": False, "error_code": "E_MISSING_INPUT"},
            note="缺失输入",
        ),
        _request(
            "edge-req-003",
            3,
            "edge_broken_ref",
            "v1",
            _delay_shapes(3),
            data_seed=seed * 1000 + 3,
            expectation={"valid": False, "error_code": "E_GRAPH_REFERENCE_MISSING"},
            note="图引用缺失",
        ),
        _request(
            "edge-req-004",
            4,
            "edge_nonpositive_dim",
            "v1",
            _delay_shapes(3),
            data_seed=seed * 1000 + 4,
            expectation={"valid": False, "error_code": "E_INVALID_DIMENSION"},
            note="图维度非正",
        ),
        _request(
            "edge-req-005",
            5,
            "edge_not_closed",
            "v1",
            _delay_shapes(3),
            data_seed=seed * 1000 + 5,
            expectation={"valid": False, "error_code": "E_OP_SHAPE_NOT_CLOSED"},
            note="算子输出形状不闭合",
        ),
        _request(
            "edge-req-006",
            6,
            "edge_valid_delay",
            "v1",
            {
                "flight_features": [3, 12],
                "weather": [3, 4],
                "slot_mask": [3, 8],
                "unexpected": [3, 1],
            },
            data_seed=seed * 1000 + 6,
            expectation={"valid": False, "error_code": "E_UNKNOWN_INPUT"},
            note="未知输入",
        ),
    ]
    project = {
        "schema_version": PROJECT_SCHEMA,
        "project_id": f"flight-edge-{seed}",
        "generator": {
            "name": "deterministic-flight-inference-generator",
            "version": GENERATOR_VERSION,
            "seed": seed,
            "fixture": "edge-cases",
        },
        "cache_config": {
            "capacity": 2,
            "policy": "LRU",
            "numeric_tolerance": 1e-6,
            "ms_per_work_unit": 0.0125,
        },
        "models": models,
        "requests": requests,
        "events": [],
        "expectations": {
            "request_expectations": {
                item["id"]: dict(item["expectation"]) for item in requests
            },
            "oracle_source": "deterministic generator fixed-seed metadata",
        },
    }
    return project


def generate_bundle(
    out_dir: str | Path = "artifacts/generated",
    *,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    """Write project, edge-case project and manifest to ``out_dir``."""

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    project = generate_project(seed)
    edge_project = generate_edge_project(seed)
    project_path = out / "project.json"
    edge_path = out / "edge_project.json"
    manifest_path = out / "manifest.json"
    atomic_write_json(project_path, project)
    atomic_write_json(edge_path, edge_project)
    manifest = {
        "generator": {
            "name": "deterministic-flight-inference-generator",
            "version": GENERATOR_VERSION,
            "seed": seed,
        },
        "project_file": "project.json",
        "project_fingerprint": "project_"
        + sha256_hex(
            {
                "seed": seed,
                "project": project,
            },
            length=24,
        ),
        "edge_project_file": "edge_project.json",
        "counts": {
            "models": len(project["models"]),
            "versions": sum(len(model["versions"]) for model in project["models"].values()),
            "requests": len(project["requests"]),
            "valid_requests": project["expectations"]["valid_request_count"],
            "invalid_requests": project["expectations"]["invalid_request_count"],
            "events": len(project["events"]),
            "edge_requests": len(edge_project["requests"]),
        },
        "expected_error_codes": {
            item["id"]: item["expectation"]["error_code"]
            for item in project["requests"]
            if not item["expectation"]["valid"]
        },
        "edge_expected_error_codes": {
            item["id"]: item["expectation"]["error_code"]
            for item in edge_project["requests"]
            if not item["expectation"]["valid"]
        },
        "request_truth": _request_truth(project),
        "edge_request_truth": _request_truth(edge_project),
        "expected_distinct_compile_keys": project["expectations"][
            "distinct_compile_key_count"
        ],
        "expected_shape_segment_distribution": project["expectations"][
            "shape_segment_distribution"
        ],
        "model_graph_fingerprints": model_graph_fingerprints(project),
        "notes": project["expectations"]["notes"],
    }
    atomic_write_json(manifest_path, manifest)
    return {
        "out_dir": str(out),
        "project": project_path,
        "edge_project": edge_path,
        "manifest": manifest_path,
        "project_data": project,
        "edge_project_data": edge_project,
        "manifest_data": manifest,
    }


def _request_truth(project: Mapping[str, Any]) -> dict[str, Any]:
    truth: dict[str, Any] = {}
    for request in project.get("requests", []):
        item: dict[str, Any] = {
            "valid": bool(request["expectation"]["valid"]),
            "error_code": request["expectation"].get("error_code"),
            "model_id": request.get("model_id"),
            "model_version": request.get("model_version"),
            "input_shapes": copy.deepcopy(request.get("input_shapes", {})),
        }
        model = project.get("models", {}).get(request.get("model_id"))
        graph = (
            get_graph(model, request.get("model_version"))
            if isinstance(model, Mapping)
            else None
        )
        if item["valid"] and graph is not None:
            item["compile_key"] = make_compile_key(
                str(request["model_id"]),
                str(request["model_version"]),
                request["input_shapes"],
            )
            item["shape_segment"] = shape_segment(graph, request["input_shapes"])
        truth[str(request["id"])] = item
    return truth
