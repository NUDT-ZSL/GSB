from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import PipelineError, error


SUPPORTED_DTYPES = ("float16", "float32", "float64")
SUPPORTED_LAYOUTS = ("row", "col")
SUPPORTED_ACCUM_MODES = ("plain", "kahan", "neumaier")
SUPPORTED_OP_KINDS = ("gemm", "bmm")


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class JobSpec:
    job_id: str
    op_kind: str
    matrix_shapes: tuple[tuple[int, ...], tuple[int, ...]]
    dtype: str
    layout: str
    seed: int
    block_size: int
    accum_mode: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "op_kind": self.op_kind,
            "matrix_shapes": [list(shape) for shape in self.matrix_shapes],
            "dtype": self.dtype,
            "layout": self.layout,
            "seed": self.seed,
            "block_size": self.block_size,
            "accum_mode": self.accum_mode,
        }

    @property
    def batch_size(self) -> int:
        return self.matrix_shapes[0][0] if self.op_kind == "bmm" else 1

    @property
    def m(self) -> int:
        return self.matrix_shapes[0][-2]

    @property
    def k(self) -> int:
        return self.matrix_shapes[0][-1]

    @property
    def n(self) -> int:
        return self.matrix_shapes[1][-1]

    @property
    def output_shape(self) -> tuple[int, ...]:
        if self.op_kind == "bmm":
            return (self.batch_size, self.m, self.n)
        return (self.m, self.n)

    def canonical_json(self) -> str:
        return json.dumps(
            self.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def _shape_error(job_id: str | None, message: str) -> PipelineError:
    return error(
        "E_SHAPE_INVALID",
        message,
        job_id=job_id,
        field="matrix_shapes",
    )


def validate_job(raw: Any, line_number: int | None = None) -> JobSpec:
    if not isinstance(raw, dict):
        raise error(
            "E_SHAPE_INVALID",
            "任务记录必须是 JSON 对象",
            field="record",
            details={"line": line_number},
        )

    job_id_value = raw.get("job_id")
    job_id = job_id_value if isinstance(job_id_value, str) and job_id_value else None
    if job_id is None:
        raise error(
            "E_JOB_INVALID",
            "job_id 必须是非空字符串",
            field="job_id",
            details={"line": line_number},
        )

    op_kind = raw.get("op_kind")
    if op_kind not in SUPPORTED_OP_KINDS:
        raise error(
            "E_OP_KIND_UNSUPPORTED",
            f"不支持的 op_kind: {op_kind!r}",
            job_id=job_id,
            field="op_kind",
        )

    dtype = raw.get("dtype")
    if dtype not in SUPPORTED_DTYPES:
        raise error(
            "E_DTYPE_UNSUPPORTED",
            f"不支持的 dtype: {dtype!r}",
            job_id=job_id,
            field="dtype",
            details={"supported": list(SUPPORTED_DTYPES)},
        )

    block_size = raw.get("block_size")
    if not _is_int(block_size) or block_size <= 0:
        raise error(
            "E_BLOCK_SIZE_INVALID",
            "block_size 必须是正整数",
            job_id=job_id,
            field="block_size",
        )

    accum_mode = raw.get("accum_mode")
    if accum_mode not in SUPPORTED_ACCUM_MODES:
        raise error(
            "E_ACCUM_MODE_UNKNOWN",
            f"未知 accum_mode: {accum_mode!r}",
            job_id=job_id,
            field="accum_mode",
            details={"supported": list(SUPPORTED_ACCUM_MODES)},
        )

    layout = raw.get("layout")
    if layout not in SUPPORTED_LAYOUTS:
        raise error(
            "E_LAYOUT_UNSUPPORTED",
            f"不支持的 layout: {layout!r}",
            job_id=job_id,
            field="layout",
            details={"supported": list(SUPPORTED_LAYOUTS)},
        )

    seed = raw.get("seed")
    if not _is_int(seed) or seed < 0:
        raise error(
            "E_SEED_INVALID",
            "seed 必须是非负整数",
            job_id=job_id,
            field="seed",
        )

    shapes_value = raw.get("matrix_shapes")
    if not isinstance(shapes_value, (list, tuple)) or len(shapes_value) != 2:
        raise _shape_error(job_id, "matrix_shapes 必须包含两个形状数组")

    parsed_shapes: list[tuple[int, ...]] = []
    expected_rank = 2 if op_kind == "gemm" else 3
    for shape_index, shape in enumerate(shapes_value):
        if not isinstance(shape, (list, tuple)) or len(shape) != expected_rank:
            raise _shape_error(
                job_id,
                f"matrix_shapes[{shape_index}] 必须是 {expected_rank} 维数组",
            )
        if not all(_is_int(dim) and dim > 0 for dim in shape):
            raise _shape_error(
                job_id,
                f"matrix_shapes[{shape_index}] 的每个维度必须是正整数",
            )
        parsed_shapes.append(tuple(shape))

    left_shape, right_shape = parsed_shapes
    if op_kind == "gemm":
        if left_shape[1] != right_shape[0]:
            raise _shape_error(
                job_id,
                "GEMM 要求 A.shape[1] == B.shape[0]",
            )
    else:
        if left_shape[0] != right_shape[0]:
            raise error(
                "E_BATCH_MISMATCH",
                "BMM 的 A/B 批次维度不一致",
                job_id=job_id,
                field="matrix_shapes.batch",
                details={
                    "a_batch": left_shape[0],
                    "b_batch": right_shape[0],
                },
            )
        if left_shape[2] != right_shape[1]:
            raise _shape_error(
                job_id,
                "BMM 要求 A.shape[2] == B.shape[1]",
            )

    return JobSpec(
        job_id=job_id,
        op_kind=op_kind,
        matrix_shapes=(left_shape, right_shape),
        dtype=dtype,
        layout=layout,
        seed=seed,
        block_size=block_size,
        accum_mode=accum_mode,
    )


def load_jobs(path: str | Path, *, validate: bool = True) -> list[JobSpec]:
    source = Path(path)
    jobs: list[JobSpec] = []
    seen: set[str] = set()
    try:
        with source.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise error(
                        "E_SHAPE_INVALID",
                        "JSONL 记录无法解析",
                        field="record",
                        details={"line": line_number, "reason": exc.msg},
                    ) from exc
                job = validate_job(raw, line_number) if validate else _raw_to_unchecked(raw)
                if job.job_id in seen:
                    raise error(
                        "E_JOB_INVALID",
                        f"重复的 job_id: {job.job_id}",
                        job_id=job.job_id,
                        field="job_id",
                        details={"line": line_number},
                    )
                seen.add(job.job_id)
                jobs.append(job)
    except FileNotFoundError as exc:
        raise error(
            "E_JOB_INVALID",
            f"任务清单不存在: {source}",
            field="jobs_path",
        ) from exc
    if not jobs:
        raise error("E_JOB_INVALID", "任务清单为空", field="jobs_path")
    return jobs


def read_job_records(path: str | Path) -> tuple[list[dict[str, Any]], str]:
    source = Path(path)
    records: list[dict[str, Any]] = []
    try:
        raw_bytes = source.read_bytes()
    except FileNotFoundError as exc:
        raise error(
            "E_JOB_INVALID",
            f"任务清单不存在: {source}",
            field="jobs_path",
        ) from exc
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error(
            "E_JOB_INVALID",
            "任务清单不是有效 UTF-8",
            field="jobs_path",
        ) from exc
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise error(
                "E_SHAPE_INVALID",
                "JSONL 记录无法解析",
                field="record",
                details={"line": line_number, "reason": exc.msg},
            ) from exc
        if not isinstance(record, dict):
            raise error(
                "E_SHAPE_INVALID",
                "任务记录必须是 JSON 对象",
                field="record",
                details={"line": line_number},
            )
        records.append(record)
    import hashlib

    checksum = f"sha256:{hashlib.sha256(raw_bytes).hexdigest()}"
    return records, checksum


def job_from_dict(payload: dict[str, Any]) -> JobSpec:
    shapes = tuple(tuple(int(value) for value in shape) for shape in payload["matrix_shapes"])
    return JobSpec(
        job_id=str(payload["job_id"]),
        op_kind=str(payload["op_kind"]),
        matrix_shapes=shapes,
        dtype=str(payload["dtype"]),
        layout=str(payload["layout"]),
        seed=int(payload["seed"]),
        block_size=int(payload["block_size"]),
        accum_mode=str(payload["accum_mode"]),
    )


def _raw_to_unchecked(raw: Any) -> JobSpec:
    if not isinstance(raw, dict):
        raise error("E_JOB_INVALID", "任务记录必须是 JSON 对象", field="record")
    return JobSpec(
        job_id=str(raw.get("job_id", "")),
        op_kind=str(raw.get("op_kind", "")),
        matrix_shapes=tuple(
            tuple(int(dim) for dim in shape) for shape in raw.get("matrix_shapes", [])
        ),
        dtype=str(raw.get("dtype", "")),
        layout=str(raw.get("layout", "")),
        seed=int(raw.get("seed", 0)),
        block_size=int(raw.get("block_size", 0)),
        accum_mode=str(raw.get("accum_mode", "")),
    )


def jobs_fingerprint(jobs: Iterable[JobSpec]) -> str:
    import hashlib

    canonical = "\n".join(
        job.canonical_json() for job in sorted(jobs, key=lambda item: item.job_id)
    )
    return f"sha256:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def atomic_write_text(path: str | Path, content: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, target)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_jobs_jsonl(path: str | Path, jobs: Iterable[JobSpec]) -> None:
    content = "".join(
        json.dumps(job.to_dict(), sort_keys=True, ensure_ascii=False) + "\n"
        for job in jobs
    )
    atomic_write_text(path, content)


def generate_sample_jobs(seed: int = 20261122) -> list[JobSpec]:
    templates = [
        {
            "job_id": "gemm-row-plain",
            "op_kind": "gemm",
            "matrix_shapes": [[24, 18], [18, 20]],
            "dtype": "float64",
            "layout": "row",
            "seed": seed + 101,
            "block_size": 5,
            "accum_mode": "plain",
        },
        {
            "job_id": "gemm-col-kahan",
            "op_kind": "gemm",
            "matrix_shapes": [[17, 13], [13, 19]],
            "dtype": "float32",
            "layout": "col",
            "seed": seed + 102,
            "block_size": 6,
            "accum_mode": "kahan",
        },
        {
            "job_id": "gemm-row-neumaier",
            "op_kind": "gemm",
            "matrix_shapes": [[11, 15], [15, 9]],
            "dtype": "float64",
            "layout": "row",
            "seed": seed + 103,
            "block_size": 4,
            "accum_mode": "neumaier",
        },
        {
            "job_id": "bmm-batched-plain",
            "op_kind": "bmm",
            "matrix_shapes": [[2, 9, 7], [2, 7, 8]],
            "dtype": "float64",
            "layout": "row",
            "seed": seed + 104,
            "block_size": 4,
            "accum_mode": "plain",
        },
        {
            "job_id": "bmm-batch-kahan",
            "op_kind": "bmm",
            "matrix_shapes": [[3, 13, 11], [3, 11, 6]],
            "dtype": "float32",
            "layout": "col",
            "seed": seed + 105,
            "block_size": 5,
            "accum_mode": "kahan",
        },
        {
            "job_id": "bmm-single-neumaier",
            "op_kind": "bmm",
            "matrix_shapes": [[1, 10, 12], [1, 12, 14]],
            "dtype": "float64",
            "layout": "row",
            "seed": seed + 106,
            "block_size": 3,
            "accum_mode": "neumaier",
        },
        {
            "job_id": "bmm-single-col-plain",
            "op_kind": "bmm",
            "matrix_shapes": [[1, 8, 8], [1, 8, 5]],
            "dtype": "float16",
            "layout": "col",
            "seed": seed + 107,
            "block_size": 4,
            "accum_mode": "plain",
        },
        {
            "job_id": "gemm-wide-kahan",
            "op_kind": "gemm",
            "matrix_shapes": [[7, 31], [31, 33]],
            "dtype": "float64",
            "layout": "row",
            "seed": seed + 108,
            "block_size": 7,
            "accum_mode": "kahan",
        },
    ]
    return [validate_job(template) for template in templates]
