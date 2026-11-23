"""Generate a deterministic local operator task manifest and NumPy samples."""

from __future__ import annotations

import argparse
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import numpy as np

from .errors import Diagnostic
from .model import TaskCase
from .util import (
    atomic_write_bytes,
    atomic_write_json,
    content_hash_array,
    ensure_dir,
    preflight_directory,
    sha256_bytes,
)


DEFAULT_SEED = 20261124


def demo_cases() -> list[TaskCase]:
    return [
        TaskCase(
            case_id="case_gelu_perm",
            op_chain=("gelu", "transpose"),
            input_shape=(2, 3, 4),
            axis_order=(0, 2, 1),
            activation_kind="gelu",
            block_size=5,
            eps=1e-8,
            dtype="float64",
            seed=1101,
            policy="fusion_first",
        ),
        TaskCase(
            case_id="case_relu_perm_f32",
            op_chain=("relu", "transpose"),
            input_shape=(4, 5, 3),
            axis_order=(2, 0, 1),
            activation_kind="relu",
            block_size=7,
            eps=1e-8,
            dtype="float32",
            seed=1102,
            policy="fusion_first",
        ),
        TaskCase(
            case_id="case_silu_eps_axis",
            op_chain=("silu", "transpose"),
            input_shape=(3, 5),
            axis_order=(1, 0),
            activation_kind="silu",
            block_size=4,
            eps=1e-6,
            dtype="float64",
            seed=1103,
            policy="fusion_first",
        ),
        TaskCase(
            case_id="case_downstream_layernorm",
            op_chain=("gelu", "transpose", "layernorm"),
            input_shape=(3, 4, 5),
            axis_order=(0, 2, 1),
            activation_kind="gelu",
            block_size=6,
            eps=1e-5,
            dtype="float32",
            seed=1104,
            policy="balanced",
        ),
        TaskCase(
            case_id="case_memory_first",
            op_chain=("tanh", "transpose"),
            input_shape=(6, 4, 3),
            axis_order=(0, 2, 1),
            activation_kind="tanh",
            block_size=8,
            eps=1e-8,
            dtype="float64",
            seed=1105,
            policy="memory_first",
        ),
        TaskCase(
            case_id="case_reduction_between",
            op_chain=("gelu", "softmax", "transpose"),
            input_shape=(2, 4, 3),
            axis_order=(0, 2, 1),
            activation_kind="gelu",
            block_size=5,
            eps=1e-8,
            dtype="float64",
            seed=1106,
            policy="fusion_first",
        ),
        TaskCase(
            case_id="case_reduction_candidate",
            op_chain=("softmax", "transpose"),
            input_shape=(3, 4),
            axis_order=(1, 0),
            activation_kind="softmax",
            block_size=4,
            eps=1e-8,
            dtype="float64",
            seed=1107,
            policy="fusion_first",
        ),
        TaskCase(
            case_id="case_identity_axis",
            op_chain=("sigmoid", "permute"),
            input_shape=(4, 3, 2),
            axis_order=(0, 1, 2),
            activation_kind="sigmoid",
            block_size=6,
            eps=1e-8,
            dtype="float64",
            seed=1108,
            policy="balanced",
        ),
        TaskCase(
            case_id="case_large_block",
            op_chain=("gelu", "transpose"),
            input_shape=(8, 16, 8),
            axis_order=(2, 1, 0),
            activation_kind="gelu",
            block_size=4096,
            eps=1e-8,
            dtype="float64",
            seed=1109,
            policy="fusion_first",
        ),
    ]


def _npz_bytes(mapping: dict[str, np.ndarray]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, array in mapping.items():
            array_buffer = io.BytesIO()
            np.lib.format.write_array(array_buffer, np.ascontiguousarray(array), allow_pickle=False)
            info = zipfile.ZipInfo(f"{name}.npy", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, array_buffer.getvalue())
    return buffer.getvalue()


def generate_dataset(
    data_dir: str | Path = "data",
    *,
    manifest_path: str | Path | None = None,
    seed: int = DEFAULT_SEED,
    case_count: int | None = None,
) -> dict[str, Any]:
    data_root = ensure_dir(data_dir)
    preflight = preflight_directory(data_root, stage="data_workspace")
    if preflight is not None:
        raise RuntimeError(preflight.to_dict())
    cases_dir = ensure_dir(data_root / "cases")
    tasks = demo_cases()
    if case_count is not None:
        tasks = tasks[: int(case_count)]
    manifest_file = Path(manifest_path) if manifest_path else data_root / "manifest.jsonl"
    ensure_dir(manifest_file.parent)

    aggregate: dict[str, np.ndarray] = {}
    manifest_lines: list[str] = []
    hashes: dict[str, str] = {}
    for task in tasks:
        # The manifest seed is the complete deterministic input seed.  The
        # global dataset seed is recorded as generation metadata/version.
        rng = np.random.default_rng(int(task.seed))
        values = rng.standard_normal(task.input_shape, dtype=np.float64)
        values = (values * 1.25 + 0.1).astype(task.dtype)
        input_file = f"cases/{task.case_id}.npz"
        task = task.evolved(input_file=input_file)
        case_bytes = _npz_bytes({"input": values})
        atomic_write_bytes(cases_dir / f"{task.case_id}.npz", case_bytes)
        aggregate[task.case_id] = values
        hashes[task.case_id] = content_hash_array(values)
        manifest_lines.append(json.dumps(task.to_dict(), sort_keys=True))

    aggregate_bytes = _npz_bytes(aggregate)
    atomic_write_bytes(data_root / "inputs.npz", aggregate_bytes)
    atomic_write_bytes(manifest_file, ("\n".join(manifest_lines) + "\n").encode("utf-8"))
    metadata = {
        "schema": "tf-local-dataset-v1",
        "dataset_seed": int(seed),
        "case_count": len(tasks),
        "manifest": str(manifest_file),
        "manifest_sha256": sha256_bytes(manifest_file.read_bytes()),
        "aggregate_inputs_sha256": sha256_bytes(aggregate_bytes),
        "input_content_sha256": hashes,
        "generator": "numpy.random.default_rng(task.seed).standard_normal",
        "dtype_policy": "manifest dtype with float64 generation then deterministic cast",
    }
    atomic_write_json(data_root / "dataset_meta.json", metadata)
    return metadata


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default="data")
    parser.add_argument("--manifest", default=None)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--case-count", type=int, default=None)
    args = parser.parse_args(argv)
    metadata = generate_dataset(
        args.data_dir,
        manifest_path=args.manifest,
        seed=args.seed,
        case_count=args.case_count,
    )
    print(json.dumps(metadata, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
