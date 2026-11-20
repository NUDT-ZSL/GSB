"""Deterministic local generation of JSONL task lists and anomaly samples."""

import json
from pathlib import Path


VALID_CASES = [
    {
        "case_id": "case_mix_small_f64",
        "op_chain": [
            {"op": "relu"},
            {"op": "transpose", "axes": [1, 0]},
            {"op": "gelu"},
            {"op": "transpose", "axes": [1, 0]},
            {"op": "sigmoid"},
        ],
        "input_shape": [8, 12],
        "axis_order": [0, 1],
        "activation_kind": "relu",
        "block_size": 32,
        "eps": 1e-8,
        "dtype": "float64",
        "seed": 20261121,
        "policy": {},
    },
    {
        "case_id": "case_three_axis_f32",
        "op_chain": [
            {"op": "gelu"},
            {"op": "transpose", "axes": [2, 0, 1]},
            {"op": "relu"},
            {"op": "transpose", "axes": [2, 1, 0]},
        ],
        "input_shape": [4, 6, 5],
        "axis_order": [1, 0, 2],
        "activation_kind": "gelu",
        "block_size": 64,
        "eps": 1e-6,
        "dtype": "float32",
        "seed": 20261122,
        "policy": {},
    },
    {
        "case_id": "case_repeat_activation_f64",
        "op_chain": [
            {"op": "sigmoid"},
            {"op": "relu"},
            {"op": "transpose", "axes": [1, 0, 2]},
            {"op": "sigmoid"},
        ],
        "input_shape": [3, 4, 2],
        "axis_order": [1, 0, 2],
        "activation_kind": "sigmoid",
        "block_size": 16,
        "eps": 1e-7,
        "dtype": "float64",
        "seed": 20261123,
        "policy": {"block_size": 24},
    },
]


def _anomaly_cases():
    base = {
        "input_shape": [4, 4],
        "axis_order": [0, 1],
        "activation_kind": "relu",
        "eps": 1e-8,
        "dtype": "float64",
        "seed": 7,
        "policy": {},
    }
    return [
        {
            **base,
            "case_id": "bad_unknown_op",
            "op_chain": [{"op": "mystery"}],
            "block_size": 16,
            "expected_code": "E_OP_UNKNOWN",
        },
        {
            **base,
            "case_id": "bad_axis_order",
            "op_chain": [{"op": "transpose", "axes": [0, 0]}],
            "block_size": 16,
            "expected_code": "E_AXIS_INVALID",
        },
        {
            **base,
            "case_id": "bad_eps",
            "op_chain": [{"op": "relu"}],
            "block_size": 16,
            "eps": 0.0,
            "expected_code": "E_PARAM_INVALID",
        },
        {
            **base,
            "case_id": "bad_dtype",
            "op_chain": [{"op": "relu"}],
            "block_size": 16,
            "dtype": "float16",
            "expected_code": "E_DTYPE_UNSUPPORTED",
        },
        {
            **base,
            "case_id": "bad_block_budget",
            "op_chain": [
                {"op": "relu"},
                {"op": "transpose", "axes": [1, 0]},
                {"op": "gelu"},
                {"op": "transpose", "axes": [1, 0]},
            ],
            "input_shape": [64, 64],
            "block_size": 1024 * 1024,
            "policy": {
                "latency_budget_ms": 1e-9,
                "max_resident_bytes": 1024 * 1024 * 1024,
            },
            "expected_code": "E_LATENCY_BUDGET",
        },
        {
            **base,
            "case_id": "bad_resource_limit",
            "op_chain": [{"op": "relu"}],
            "input_shape": [512, 512],
            "block_size": 1024,
            "policy": {"max_resident_bytes": 4096, "allow_degrade": False},
            "expected_code": "E_RESOURCE_LIMIT",
        },
    ]


def write_jsonl(path, records):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return path


def generate_all(samples_dir="samples"):
    samples_dir = Path(samples_dir)
    valid_path = write_jsonl(samples_dir / "tasks.valid.jsonl", VALID_CASES)
    anomaly_path = write_jsonl(
        samples_dir / "tasks.anomaly.jsonl", _anomaly_cases()
    )
    return valid_path, anomaly_path
