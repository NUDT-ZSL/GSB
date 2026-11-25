from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from .contracts import NUMPY_DTYPES, normalize_task
from .hashing import atomic_write_bytes, canonical_bytes


def input_array_from_seed(task: dict[str, Any]) -> np.ndarray:
    shape = tuple(int(dim) for dim in task["input_shape"])
    rng = np.random.default_rng(int(task["seed"]))
    dtype = NUMPY_DTYPES[task["dtype"]]
    return rng.uniform(-1.0, 1.0, size=shape).astype(dtype)


def weight_array_from_seed(task: dict[str, Any]) -> np.ndarray:
    if task["op_kind"] != "conv2d":
        raise ValueError("only conv2d tasks have generated weights")
    shape = tuple(int(dim) for dim in task["kernel_shape"])
    rng = np.random.default_rng(int(task["weight_seed"]))
    dtype = NUMPY_DTYPES[task["dtype"]]
    return (rng.standard_normal(size=shape) * 0.35).astype(dtype)


def write_matrix(path: str | Path, array: np.ndarray, dtype_name: str) -> Path:
    target = Path(path)
    le_array = np.ascontiguousarray(array).astype(NUMPY_DTYPES[dtype_name], copy=False)
    atomic_write_bytes(target, le_array.tobytes(order="C"))
    return target


def read_matrix(path: str | Path, shape: tuple[int, ...], dtype_name: str) -> np.ndarray:
    raw = Path(path).read_bytes()
    dtype = np.dtype(NUMPY_DTYPES[dtype_name])
    expected = int(np.prod(shape)) * dtype.itemsize
    if len(raw) != expected:
        raise ValueError(
            f"matrix {path} has {len(raw)} bytes; expected {expected} for shape {shape} and {dtype_name}"
        )
    return np.frombuffer(raw, dtype=dtype).reshape(shape).copy()


def _resolve_data_path(value: str | None, job_id: str, base_dir: Path | None) -> Path | None:
    if value:
        path = Path(value)
        if path.is_absolute():
            return path if path.exists() else None
        candidates = []
        if base_dir is not None:
            candidates.append(base_dir / path)
        candidates.extend([Path.cwd() / path, path])
        for candidate in candidates:
            if candidate.exists():
                return candidate
        return None
    if base_dir is not None:
        conventional = base_dir / "inputs" / f"{job_id}.bin"
        if conventional.exists():
            return conventional
    return None


def load_arrays(task: dict[str, Any], base_dir: str | Path | None = None) -> dict[str, np.ndarray]:
    base = Path(base_dir) if base_dir is not None else None
    job_id = task["job_id"]
    input_path = _resolve_data_path(task.get("input_file"), job_id, base)
    if input_path is not None:
        input_array = read_matrix(input_path, tuple(task["input_shape"]), task["dtype"])
    else:
        input_array = input_array_from_seed(task)

    arrays: dict[str, np.ndarray] = {"input": input_array}
    if task["op_kind"] == "conv2d":
        weight_path = _resolve_data_path(task.get("weight_file"), job_id, base)
        if weight_path is not None:
            arrays["weight"] = read_matrix(
                weight_path, tuple(task["kernel_shape"]), task["dtype"]
            )
        else:
            arrays["weight"] = weight_array_from_seed(task)
    return arrays


def _task(
    job_id: str,
    op_kind: str,
    shape: list[int],
    seed: int,
    *,
    revision: int = 1,
    dtype: str = "float64",
    kernel_shape: list[int] | int | None = None,
    scale: float | list[float] | None = None,
    stride: int | list[int] = 1,
    padding: int | list[int] | str = 0,
    interp_mode: str | None = None,
    policy: dict[str, Any] | str | None = None,
    depends_on: list[str] | None = None,
    input_file: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "job_id": job_id,
        "op_kind": op_kind,
        "input_shape": shape,
        "stride": stride,
        "padding": padding,
        "seed": seed,
        "weight_seed": seed + 10_000,
        "dtype": dtype,
        "block_policy": policy or {"candidates": [[4, 4]], "selection": "balanced", "boundary": "zero"},
        "revision": revision,
    }
    if kernel_shape is not None:
        record["kernel_shape"] = kernel_shape
    if scale is not None:
        record["scale"] = scale
    if interp_mode is not None:
        record["interp_mode"] = interp_mode
    if depends_on:
        record["depends_on"] = depends_on
    record["input_file"] = input_file or f"inputs/{job_id}.bin"
    if op_kind == "conv2d":
        record["weight_file"] = f"weights/{job_id}.bin"
    return record


def base_sample_tasks() -> list[dict[str, Any]]:
    return [
        _task(
            "conv_basic", "conv2d", [1, 1, 8, 8], 1001,
            kernel_shape=[3, 3], padding=1,
            policy={"candidates": [[4, 4], [8, 8]], "selection": "min_waste", "boundary": "zero"},
        ),
        _task(
            "conv_stride", "conv2d", [1, 2, 9, 7], 1002, dtype="float32",
            kernel_shape=[3, 3], stride=2, padding=1,
            policy={"candidates": [[3, 3], [5, 5]], "selection": "balanced", "boundary": "replicate"},
        ),
        _task(
            "interp_nearest", "interpolate", [1, 1, 6, 7], 1003,
            scale=[2.0, 2.0], interp_mode="nearest",
            policy={"candidates": [[4, 4]], "selection": "largest_fit", "boundary": "replicate"},
        ),
        _task(
            "interp_bilinear", "interpolate", [1, 1, 7, 6], 1004,
            scale=[1.5, 0.5], interp_mode="bilinear",
            policy={"candidates": [[3, 3], [5, 5]], "selection": "min_waste", "boundary": "zero"},
        ),
        _task(
            "interp_bicubic", "interpolate", [1, 1, 8, 8], 1005,
            scale=[0.75, 1.25], interp_mode="bicubic",
            policy={"candidates": [[4, 4], [6, 6]], "selection": "balanced", "boundary": "reflect"},
        ),
        _task(
            "inc_block", "conv2d", [1, 1, 8, 8], 1101,
            kernel_shape=[3, 3], padding=1,
            policy={"candidates": [[4, 4], [8, 8]], "selection": "largest_fit", "boundary": "zero"},
        ),
        _task(
            "inc_block_down", "interpolate", [1, 1, 5, 6], 1102,
            scale=1.5, interp_mode="bilinear", depends_on=["inc_block"],
            policy={"candidates": [[4, 4]], "selection": "min_waste", "boundary": "zero"},
        ),
        _task(
            "inc_geom", "conv2d", [1, 1, 9, 9], 1201,
            kernel_shape=[3, 3], stride=1, padding=1,
            policy={"candidates": [[4, 4]], "selection": "largest_fit", "boundary": "zero"},
        ),
        _task(
            "inc_geom_down", "interpolate", [1, 1, 6, 6], 1202,
            scale=1.5, interp_mode="bilinear", depends_on=["inc_geom"],
            policy={"candidates": [[4, 4]], "selection": "min_waste", "boundary": "replicate"},
        ),
        _task(
            "inc_shape", "interpolate", [1, 1, 8, 8], 1301,
            scale=0.5, interp_mode="bilinear",
            policy={"candidates": [[3, 3], [5, 5]], "selection": "balanced", "boundary": "zero"},
            input_file="inputs/inc_shape_r1.bin",
        ),
        _task(
            "inc_shape_down", "conv2d", [1, 1, 5, 5], 1302,
            kernel_shape=[3, 3], padding=1, depends_on=["inc_shape"],
            policy={"candidates": [[3, 3]], "selection": "largest_fit", "boundary": "zero"},
        ),
    ]


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    lines = [json.dumps(record, ensure_ascii=False, sort_keys=True) for record in records]
    from .hashing import atomic_write_bytes
    atomic_write_bytes(path, ("\n".join(lines) + "\n").encode("utf-8"))


def generate_sample_set(out_dir: str | Path) -> dict[str, Any]:
    root = Path(out_dir)
    (root / "inputs").mkdir(parents=True, exist_ok=True)
    (root / "weights").mkdir(parents=True, exist_ok=True)

    tasks = base_sample_tasks()
    normalized = [normalize_task(record) for record in tasks]
    for raw, task in zip(tasks, normalized):
        x = input_array_from_seed(task)
        write_matrix(root / raw["input_file"], x, task["dtype"])
        if task["op_kind"] == "conv2d":
            w = weight_array_from_seed(task)
            write_matrix(root / raw["weight_file"], w, task["dtype"])

    shape_r2_raw = next(record for record in tasks if record["job_id"] == "inc_shape").copy()
    shape_r2_raw["revision"] = 2
    shape_r2_raw["input_shape"] = [1, 1, 10, 10]
    shape_r2_raw["input_file"] = "inputs/inc_shape_r2.bin"
    shape_r2_task = normalize_task(shape_r2_raw)
    write_matrix(root / shape_r2_raw["input_file"], input_array_from_seed(shape_r2_task), "float64")

    block_r2 = next(record for record in tasks if record["job_id"] == "inc_block").copy()
    block_r2["revision"] = 2
    block_r2["block_policy"] = {
        "candidates": [[3, 3], [5, 5]],
        "selection": "balanced",
        "boundary": "zero",
    }

    geom_r2 = next(record for record in tasks if record["job_id"] == "inc_geom").copy()
    geom_r2["revision"] = 2
    geom_r2["stride"] = 2
    geom_r2["padding"] = 0

    shape_r2 = shape_r2_raw

    invalid_cases = [
        {
            "case": "shape",
            "expect_code": "E_SHAPE_INVALID",
            "record": _task("invalid_shape", "conv2d", [1, 0, 8, 8], 9001, kernel_shape=[3, 3]),
        },
        {
            "case": "dtype",
            "expect_code": "E_DTYPE_UNSUPPORTED",
            "record": {**_task("invalid_dtype", "conv2d", [1, 1, 8, 8], 9002, kernel_shape=[3, 3]), "dtype": "float16"},
        },
        {
            "case": "padding",
            "expect_code": "E_PADDING_INVALID",
            "record": {**_task("invalid_padding", "conv2d", [1, 1, 8, 8], 9003, kernel_shape=[3, 3]), "padding": -1},
        },
        {
            "case": "stride",
            "expect_code": "E_STRIDE_INVALID",
            "record": {**_task("invalid_stride", "conv2d", [1, 1, 8, 8], 9004, kernel_shape=[3, 3]), "stride": 0},
        },
        {
            "case": "interp_mode",
            "expect_code": "E_INTERP_MODE_UNSUPPORTED",
            "record": {
                **_task("invalid_mode", "interpolate", [1, 1, 8, 8], 9005, scale=2.0),
                "interp_mode": "cubic4",
            },
        },
        {
            "case": "revision_rollback",
            "expect_code": "E_REVISION_ROLLBACK",
            "prior_revision": 3,
            "record": {**next(r for r in tasks if r["job_id"] == "conv_basic"), "revision": 2},
        },
    ]

    _write_jsonl(root / "tasks_v1.jsonl", tasks)
    _write_jsonl(root / "update_block_policy.jsonl", [block_r2])
    _write_jsonl(root / "update_geometry.jsonl", [geom_r2])
    _write_jsonl(root / "update_shape.jsonl", [shape_r2])
    with (root / "invalid_cases.jsonl").open("w", encoding="utf-8") as handle:
        for case in invalid_cases:
            handle.write(json.dumps(case, ensure_ascii=False, sort_keys=True) + "\n")

    manifest = {
        "sample": "opverify-fixed-seed",
        "seed_policy": "np.random.default_rng(job.seed/weight_seed)",
        "task_count": len(tasks),
        "files": {
            "tasks": "tasks_v1.jsonl",
            "block_update": "update_block_policy.jsonl",
            "geometry_update": "update_geometry.jsonl",
            "shape_update": "update_shape.jsonl",
            "invalid_cases": "invalid_cases.jsonl",
        },
        "tasks_digest": __import__("opverify.hashing", fromlist=["sha256_bytes"]).sha256_bytes(
            canonical_bytes(tasks)
        ),
    }
    from .hashing import atomic_write_json
    atomic_write_json(root / "sample_manifest.json", manifest)
    return manifest


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    with Path(path).open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            line = line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                from .errors import OpVerifyError
                raise OpVerifyError(
                    "E_MANIFEST_INVALID",
                    f"invalid JSON on line {line_number}: {exc.msg}",
                    field=f"$.jsonl[{line_number}]",
                ) from exc
            records.append(value)
    return records
