#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "samples"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_json(value: Any) -> str:
    return digest_bytes(canonical_json(value).encode("utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


LAYER_LAYOUT = {
    "encoder.0": {
        "param_count": 64,
        "shards": [16, 24, 24],
    },
    "encoder.1": {
        "param_count": 96,
        "shards": [32, 32, 32],
    },
    "head": {
        "param_count": 32,
        "shards": [16, 16],
    },
}


def deterministic_array(version: int, layer: str, count: int) -> np.ndarray:
    seed_bytes = hashlib.sha256(f"hotgray|v{version}|{layer}".encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(seed_bytes[:8], "big"))
    base = rng.standard_normal(count, dtype=np.float32) * np.float32(0.05)
    return base.astype("<f4")


def build_version(
    version: int,
    arrays: dict[str, np.ndarray],
) -> tuple[list[dict[str, Any]], dict[tuple[str, int], dict[str, Any]]]:
    layers: list[dict[str, Any]] = []
    shard_index: dict[tuple[str, int], dict[str, Any]] = {}
    for layer_name in sorted(LAYER_LAYOUT):
        layout = LAYER_LAYOUT[layer_name]
        array = arrays[layer_name]
        layer_shards: list[dict[str, Any]] = []
        cursor = 0
        for index, shard_params in enumerate(layout["shards"]):
            start = cursor * 4
            end = (cursor + shard_params) * 4
            data = array[cursor : cursor + shard_params].tobytes()
            relative_path = f"weights/v{version}/{layer_name}/shard-{index:03d}.bin"
            target_path = SAMPLES / relative_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_bytes(data)
            shard = {
                "shard_index": index,
                "byte_start": start,
                "byte_end": end,
                "digest": digest_bytes(data),
                "path": relative_path,
            }
            layer_shards.append(shard)
            shard_index[(layer_name, index)] = shard
            cursor += shard_params
        if cursor != layout["param_count"]:
            raise AssertionError((layer_name, cursor, layout["param_count"]))
        layers.append(
            {
                "name": layer_name,
                "param_count": layout["param_count"],
                "shards": layer_shards,
            }
        )
    return layers, shard_index


def make_resource(
    *,
    resource_id: str,
    kind: str,
    revision: str,
    depends_on: list[str],
    params: dict[str, Any],
) -> dict[str, Any]:
    body = {
        "id": resource_id,
        "kind": kind,
        "revision": revision,
        "depends_on": depends_on,
        "params": params,
    }
    return {**body, "digest": digest_json(body)}


def request_bucket(request_id: str) -> float:
    return int(hashlib.sha256(request_id.encode("utf-8")).hexdigest()[:16], 16) / float(16**16)


def choose_request_id(prefix: str, *, under: float | None = None, at_least: float | None = None) -> str:
    for index in range(10000):
        candidate = f"{prefix}-{index:04d}"
        bucket = request_bucket(candidate)
        if under is None and at_least is None:
            return candidate
        if under is not None and bucket < under:
            return candidate
        if at_least is not None and bucket >= at_least:
            return candidate
    raise RuntimeError("unable to find deterministic request id")


def main() -> None:
    if SAMPLES.exists():
        for directory in ("weights", "patches"):
            generated_dir = SAMPLES / directory
            if generated_dir.exists():
                shutil.rmtree(generated_dir)
        for filename in (
            "models.json",
            "replicas.json",
            "plan.json",
            "clock_steps.json",
            "generation_manifest.json",
        ):
            generated_file = SAMPLES / filename
            if generated_file.exists():
                generated_file.unlink()
    (SAMPLES / "patches").mkdir(parents=True)

    v1_arrays = {
        layer: deterministic_array(1, layer, layout["param_count"])
        for layer, layout in LAYER_LAYOUT.items()
    }
    v2_arrays = {layer: array.copy() for layer, array in v1_arrays.items()}
    v2_arrays["encoder.1"][32] = np.float32(v2_arrays["encoder.1"][32] + 0.125)
    v2_arrays["head"][0] = np.float32(v2_arrays["head"][0] - 0.25)
    v2_arrays["head"][15] = np.float32(v2_arrays["head"][15] * 0.5)

    v1_layers, v1_shards = build_version(1, v1_arrays)
    v2_layers, v2_shards = build_version(2, v2_arrays)

    resources = [
        make_resource(
            resource_id="upstream:v1",
            kind="upstream_model",
            revision="upstream-1",
            depends_on=[],
            params={"fixture": "ranking-upstream-v1"},
        ),
        make_resource(
            resource_id="upstream:v2",
            kind="upstream_model",
            revision="upstream-2",
            depends_on=[],
            params={"fixture": "ranking-upstream-v2"},
        ),
        make_resource(
            resource_id="feature:v1",
            kind="feature_table",
            revision="features-1",
            depends_on=["upstream:v1"],
            params={"feature_width": 8, "revision": 1},
        ),
        make_resource(
            resource_id="feature:v2",
            kind="feature_table",
            revision="features-2",
            depends_on=["upstream:v2"],
            params={"feature_width": 8, "revision": 2},
        ),
        make_resource(
            resource_id="pre:v1",
            kind="preprocess",
            revision="pre-1",
            depends_on=["feature:v1"],
            params={"scale": 1.0, "offset": 0.0, "rotation": 0},
        ),
        make_resource(
            resource_id="pre:v2",
            kind="preprocess",
            revision="pre-2",
            depends_on=["feature:v2"],
            params={"scale": 1.125, "offset": 0.03125, "rotation": 1},
        ),
        make_resource(
            resource_id="post:v1",
            kind="postprocess",
            revision="post-1",
            depends_on=["upstream:v1"],
            params={"scale": 0.75, "offset": 0.0625, "clip": 4.0},
        ),
    ]
    models = [
        {
            "version": 1,
            "layers": v1_layers,
            "preprocess": "pre:v1",
            "postprocess": "post:v1",
            "feature_table": "feature:v1",
            "upstream_model": "upstream:v1",
        },
        {
            "version": 2,
            "layers": v2_layers,
            "preprocess": "pre:v2",
            "postprocess": "post:v1",
            "feature_table": "feature:v2",
            "upstream_model": "upstream:v2",
        },
    ]
    write_json(SAMPLES / "models.json", {"resources": resources, "models": models})

    loaded_shards = {
        layer: [shard["digest"] for shard in layer_data["shards"]]
        for layer, layer_data in (
            (layer["name"], layer) for layer in v1_layers
        )
    }
    replicas = [
        {
            "replica_id": replica_id,
            "loaded_version": 1,
            "loaded_shards": loaded_shards,
            "warmup_state": "prewarmed",
            "gray_ratio": 0.0,
        }
        for replica_id in ("replica-a", "replica-b", "replica-control")
    ]
    write_json(SAMPLES / "replicas.json", replicas)

    changed_keys = [
        key
        for key in sorted(v2_shards)
        if v1_shards[key]["digest"] != v2_shards[key]["digest"]
    ]
    patch_entries = []
    for layer_name, index in changed_keys:
        shard = v2_shards[(layer_name, index)]
        patch_entries.append(
            {
                "layer": layer_name,
                "shard_index": index,
                "byte_start": shard["byte_start"],
                "byte_end": shard["byte_end"],
                "digest": shard["digest"],
            }
        )
    patch = {
        "patch_id": "patch-v1-to-v2",
        "baseline_version": 1,
        "target_version": 2,
        "entries": patch_entries,
    }
    write_json(SAMPLES / "patches" / "v1_to_v2.json", patch)

    plan = {
        "round_id": "round-001",
        "baseline_version": 1,
        "target_version": 2,
        "patch_id": "patch-v1-to-v2",
        "patch_path": "patches/v1_to_v2.json",
        "replica_ids": ["replica-a", "replica-b"],
        "start_at_ms": 1000,
        "warmup_duration_ms": 100,
        "warmup_timeout_ms": 200,
        "rollback_window_ms": 500,
        "rollback_step_ms": 100,
        "clock_tolerance_ms": 20,
        "stages": [
            {"level": 1, "ratio": 0.1, "at_ms": 1200},
            {"level": 2, "ratio": 0.5, "at_ms": 1300},
            {"level": 3, "ratio": 1.0, "at_ms": 1400},
        ],
    }
    write_json(SAMPLES / "plan.json", plan)

    target_at_ten_percent = choose_request_id("canary-target", under=0.1)
    baseline_at_ten_percent = choose_request_id("canary-baseline", at_least=0.1)
    target_at_full = choose_request_id("full-target")
    clock_steps = {
        "normal": [
            {"at_ms": 1000, "command": "patch"},
            {"at_ms": 1000, "command": "warmup"},
            {"at_ms": 1100, "command": "tick"},
            {
                "at_ms": 1200,
                "command": "stage",
                "args": {"level": 1},
            },
            {
                "at_ms": 1210,
                "command": "request",
                "args": {
                    "replica_id": "replica-a",
                    "request_id": target_at_ten_percent,
                    "input": [1.0, 0.5, -0.25, 0.125, 0.75],
                },
            },
            {
                "at_ms": 1220,
                "command": "request",
                "args": {
                    "replica_id": "replica-b",
                    "request_id": baseline_at_ten_percent,
                    "input": [1.0, 0.5, -0.25, 0.125, 0.75],
                },
            },
            {"at_ms": 1300, "command": "stage", "args": {"level": 2}},
            {
                "at_ms": 1310,
                "command": "complete",
                "args": {"request_id": target_at_ten_percent},
            },
            {
                "at_ms": 1320,
                "command": "complete",
                "args": {"request_id": baseline_at_ten_percent},
            },
            {"at_ms": 1400, "command": "stage", "args": {"level": 3}},
            {
                "at_ms": 1410,
                "command": "request",
                "args": {
                    "replica_id": "replica-a",
                    "request_id": target_at_full,
                    "input": [-1.0, 0.25, 0.5, 0.75, 1.25],
                },
            },
            {
                "at_ms": 1420,
                "command": "complete",
                "args": {"request_id": target_at_full},
            },
            {"at_ms": 1430, "command": "status"},
        ],
        "rollback": [
            {"at_ms": 1000, "command": "patch"},
            {"at_ms": 1000, "command": "warmup"},
            {"at_ms": 1100, "command": "tick"},
            {"at_ms": 1200, "command": "stage", "args": {"level": 1}},
            {"at_ms": 1300, "command": "stage", "args": {"level": 2}},
            {
                "at_ms": 1320,
                "command": "abort",
                "args": {"reason": "injected_failure", "replica_id": "replica-b"},
            },
            {"at_ms": 1420, "command": "tick"},
            {"at_ms": 1520, "command": "tick"},
            {"at_ms": 1530, "command": "status"},
        ],
        "rollback_missed": [
            {"at_ms": 1000, "command": "patch"},
            {"at_ms": 1000, "command": "warmup"},
            {"at_ms": 1100, "command": "tick"},
            {"at_ms": 1200, "command": "stage", "args": {"level": 1}},
            {"at_ms": 1300, "command": "stage", "args": {"level": 2}},
            {
                "at_ms": 1320,
                "command": "abort",
                "args": {"reason": "injected_failure"},
            },
            {"at_ms": 1900, "command": "tick"},
            {"at_ms": 1910, "command": "status"},
        ],
        "warmup_timeout": [
            {"at_ms": 1000, "command": "patch"},
            {"at_ms": 1000, "command": "warmup"},
            {"at_ms": 1250, "command": "tick"},
            {"at_ms": 1260, "command": "status"},
        ],
    }
    write_json(SAMPLES / "clock_steps.json", clock_steps)
    write_json(
        SAMPLES / "generation_manifest.json",
        {
            "generator": "scripts/generate_samples.py",
            "changed_shards": [
                {
                    "layer": layer,
                    "shard_index": index,
                    "digest": v2_shards[(layer, index)]["digest"],
                }
                for layer, index in changed_keys
            ],
            "deterministic_request_ids": {
                "target_at_10_percent": target_at_ten_percent,
                "baseline_at_10_percent": baseline_at_ten_percent,
                "target_at_full": target_at_full,
            },
        },
    )
    print(f"Generated samples under {SAMPLES}")
    print(f"Changed shards: {changed_keys}")
    print(
        "Request ids:",
        target_at_ten_percent,
        baseline_at_ten_percent,
        target_at_full,
    )


if __name__ == "__main__":
    main()
