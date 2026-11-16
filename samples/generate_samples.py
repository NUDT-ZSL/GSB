"""Regenerate deterministic graph/edit/input samples without third-party tools."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np


HERE = Path(__file__).resolve().parent
GRAPH_SEED = 20261117


def _weights(rng: np.random.Generator, shape: tuple[int, ...]) -> list:
    return np.round(rng.uniform(-0.35, 0.35, size=shape), 8).tolist()


def _bias(rng: np.random.Generator, size: int) -> list[float]:
    return np.round(rng.uniform(-0.15, 0.15, size=size), 8).tolist()


def conv(node_id: str, tensor: str, kernel: list[int], stride: list[int], padding: list[int], in_channels: int, out_channels: int, rng: np.random.Generator) -> dict:
    return {
        "id": node_id,
        "op": "conv2d",
        "tensor": tensor,
        "enabled": True,
        "params": {
            "kernel_size": kernel,
            "stride": stride,
            "padding": padding,
            "in_channels": in_channels,
            "out_channels": out_channels,
            "weights": _weights(rng, (out_channels, in_channels, kernel[0], kernel[1])),
            "bias": _bias(rng, out_channels),
        },
    }


def interpolation(node_id: str, tensor: str, op: str, align_mode: str, output_size: list[int], rng: np.random.Generator | None = None) -> dict:
    del rng
    return {
        "id": node_id,
        "op": op,
        "tensor": tensor,
        "enabled": True,
        "params": {
            "output_size": output_size,
            "align_mode": align_mode,
        },
    }


def main() -> None:
    rng = np.random.Generator(np.random.PCG64(GRAPH_SEED))
    conv1 = conv("conv1", "conv1", [3, 3], [1, 1], [1, 1], 2, 2, rng)
    conv3 = conv("conv3", "conv3", [3, 3], [2, 2], [1, 1], 2, 2, rng)
    interp_bi = interpolation("interp_bi", "interp_bi", "bilinear", "half_pixel", [13, 15], rng)
    interp_nn = interpolation("interp_nn", "interp_nn", "nearest", "half_pixel", [7, 8], rng)
    conv2 = conv("conv2", "conv2", [3, 3], [1, 1], [0, 0], 2, 3, rng)
    graph = {
        "schema_version": 1,
        "graph_id": "local_feature_pipeline",
        "input_tensors": {"input0": {"shape": [1, 2, 9, 10], "dtype": "float64"}},
        "nodes": [conv1, conv3, interp_bi, interp_nn, conv2],
        "edges": [
            {"id": "e_input_conv1", "from": "input0", "to": "conv1", "tensor": "input0"},
            {"id": "e_input_conv3", "from": "input0", "to": "conv3", "tensor": "input0"},
            {"id": "e_conv1_bi", "from": "conv1", "to": "interp_bi", "tensor": "conv1"},
            {"id": "e_conv3_nn", "from": "conv3", "to": "interp_nn", "tensor": "conv3"},
            {"id": "e_bi_conv2", "from": "interp_bi", "to": "conv2", "tensor": "interp_bi"},
        ],
        "outputs": ["conv2", "interp_nn"],
    }
    (HERE / "graph.json").write_text(json.dumps(graph, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    input_descriptor = {
        "schema_version": 1,
        "tensor_id": "input0",
        "shape": [1, 2, 9, 10],
        "dtype": "float64",
        "layout": "NCHW",
        "generator": {
            "kind": "numpy_pcg64",
            "seed": 20261117,
            "distribution": {"kind": "uniform", "low": -1.0, "high": 1.0},
        },
        "special_values": [
            {"index": [0, 0, 0, 0], "value": -3.0},
            {"index": [0, 0, 2, 3], "value": 0.0},
            {"index": [0, 1, 5, 6], "value": 2.5},
            {"index": [0, 0, 8, 9], "value": 4.0},
        ],
    }
    (HERE / "input.json").write_text(json.dumps(input_descriptor, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    rng_edit = np.random.Generator(np.random.PCG64(20261118))
    conv3b = conv("conv3b", "conv3b", [3, 3], [2, 2], [1, 1], 2, 2, rng_edit)
    interp_nn_b = interpolation("interp_nn_b", "interp_nn_b", "nearest", "asymmetric", [7, 8], rng_edit)
    interp_extra = interpolation("interp_extra", "interp_extra", "bilinear", "align_corners", [9, 10], rng_edit)
    edits = {
        "schema_version": 1,
        "description": "Parameter, alignment, subgraph replacement, deletion, and addition.",
        "operations": [
            {
                "id": "EDIT-001",
                "op": "update_params",
                "description": "Change conv1 bias; interp_bi and conv2 must be invalidated.",
                "node_id": "conv1",
                "params": {"bias": [0.21, -0.17]},
            },
            {
                "id": "EDIT-002",
                "op": "update_params",
                "description": "Change bilinear alignment; interp_bi and conv2 must be invalidated, conv3 branch reused.",
                "node_id": "interp_bi",
                "params": {"align_mode": "asymmetric"},
            },
            {
                "id": "EDIT-003",
                "op": "replace_subgraph",
                "description": "Replace the nearest-neighbor branch with new nodes.",
                "remove_nodes": ["conv3", "interp_nn"],
                "add_nodes": [conv3b, interp_nn_b],
                "add_edges": [
                    {"id": "e_input_conv3b", "from": "input0", "to": "conv3b", "tensor": "input0"},
                    {"id": "e_conv3b_nnb", "from": "conv3b", "to": "interp_nn_b", "tensor": "conv3b"},
                ],
                "outputs": ["conv2", "interp_nn_b"],
            },
            {
                "id": "EDIT-004",
                "op": "delete_node",
                "description": "Delete leaf conv2 and keep the interpolated output.",
                "node_id": "conv2",
            },
            {
                "id": "EDIT-005",
                "op": "add_node",
                "description": "Add a bilinear align_corners branch and publish it as an output.",
                "node": interp_extra,
                "edges": [
                    {"id": "e_conv3b_extra", "from": "conv3b", "to": "interp_extra", "tensor": "conv3b"}
                ],
                "outputs": ["interp_nn_b", "interp_extra"],
            },
        ],
    }
    (HERE / "edits.json").write_text(json.dumps(edits, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
