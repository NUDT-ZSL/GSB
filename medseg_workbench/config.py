from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from .errors import (
    E_CHUNK,
    E_DIMENSION,
    E_JSON,
    E_MEMORY_BUDGET,
    E_MORPHOLOGY,
    E_PARALLELISM,
    E_SEED,
    E_SPACING,
    E_TASK_SCHEMA,
    E_THRESHOLD,
    E_TIME_BUDGET,
    E_VOLUME,
    WorkbenchError,
)
from .geometry import chunk_grid
from .io_utils import canonical_digest, load_json


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_positive_int(value: Any) -> bool:
    return _is_int(value) and value > 0


def _finite_positive_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _dig(d: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in d and d[key] is not None:
            return d[key]
    return None


def _as_int_tuple(value: Any, length: int, code: str, name: str) -> Tuple[int, ...]:
    if not isinstance(value, (list, tuple)) or len(value) != length:
        raise WorkbenchError(code, f"{name} must be a list of {length} integers")
    result: List[int] = []
    for item in value:
        if not _is_int(item):
            raise WorkbenchError(code, f"{name} must contain only integers")
        result.append(int(item))
    return tuple(result)


@dataclass(frozen=True)
class MorphOperation:
    op: str
    kernel: int
    iterations: int = 1

    @property
    def radius(self) -> int:
        return (self.kernel // 2) * self.iterations

    def to_dict(self) -> Dict[str, Any]:
        return {"op": self.op, "kernel": self.kernel, "iterations": self.iterations}


@dataclass
class TaskConfig:
    task_path: Path
    base_dir: Path
    raw: Dict[str, Any]
    seed: int
    shape: Tuple[int, int, int]
    spacing: Tuple[float, float, float]
    chunk_size: Tuple[int, int, int]
    threshold: Tuple[float, float]
    seed_stride: Tuple[int, int, int]
    growth_radius: int
    morph_operations: Tuple[MorphOperation, ...]
    parallelism: int
    logic_time_budget_ms: float
    pixel_memory_budget: int
    volume_dir: Path
    truth_dir: Path
    state_dir: Path
    temp_dir: Path
    export_dir: Path
    volume_pattern: str
    grid_count: Tuple[int, int, int] = field(init=False)
    morph_radius: int = field(init=False)

    def __post_init__(self) -> None:
        nz = (self.shape[0] + self.chunk_size[0] - 1) // self.chunk_size[0]
        ny = (self.shape[1] + self.chunk_size[1] - 1) // self.chunk_size[1]
        nx = (self.shape[2] + self.chunk_size[2] - 1) // self.chunk_size[2]
        self.grid_count = (nz, ny, nx)
        self.morph_radius = sum(op.radius for op in self.morph_operations)

    @property
    def chunk_count(self) -> int:
        return int(np.prod(self.grid_count))

    def normalized_parameters(self) -> Dict[str, Any]:
        """Canonical JSON-safe parameter subset used for versioning."""
        return {
            "schema_version": str(self.raw.get("schema_version", "1.0")),
            "seed": self.seed,
            "shape": list(self.shape),
            "spacing": list(self.spacing),
            "chunk_size": list(self.chunk_size),
            "region_growth": {
                "threshold": list(self.threshold),
                "seed_stride": list(self.seed_stride),
                "growth_radius": self.growth_radius,
            },
            "morphology": [op.to_dict() for op in self.morph_operations],
            "parallelism": self.parallelism,
            "logic_time_budget_ms": self.logic_time_budget_ms,
            "pixel_memory_budget": self.pixel_memory_budget,
            "paths": {
                "volume_dir": self.raw.get("volume_dir", "volume"),
                "truth_dir": self.raw.get("truth_dir", "truth"),
                "state_dir": self.raw.get("state_dir", ".medseg_state"),
                "temp_dir": self.raw.get("temp_dir", "tmp"),
                "export_dir": self.raw.get("export_dir", "exports"),
                "volume_pattern": self.volume_pattern,
            },
        }


def parse_task(task_path: Path | str) -> TaskConfig:
    task_path = Path(task_path).expanduser().resolve()
    if not task_path.is_file():
        raise WorkbenchError(E_JSON, f"task file does not exist: {task_path}")
    try:
        raw = load_json(task_path)
    except Exception as exc:
        raise WorkbenchError(E_JSON, f"cannot parse task JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise WorkbenchError(E_TASK_SCHEMA, "task JSON root must be an object")

    shape_value = _dig(raw, "shape", "volume_shape", "dimensions")
    shape = _as_int_tuple(shape_value, 3, E_DIMENSION, "shape")
    if any(dim <= 0 or not math.isfinite(dim) for dim in shape):
        raise WorkbenchError(E_DIMENSION, "shape dimensions must be finite positive integers")

    spacing_value = _dig(raw, "spacing", "voxel_spacing", "voxel_spacing_zyx")
    if spacing_value is None:
        spacing_value = [1.0, 1.0, 1.0]
    if not isinstance(spacing_value, (list, tuple)) or len(spacing_value) != 3:
        raise WorkbenchError(E_SPACING, "spacing must contain three finite positive numbers")
    try:
        spacing = tuple(float(v) for v in spacing_value)  # type: ignore[assignment]
    except (TypeError, ValueError) as exc:
        raise WorkbenchError(E_SPACING, "spacing must contain finite positive numbers") from exc
    if any(not math.isfinite(v) or v <= 0 for v in spacing):
        raise WorkbenchError(E_SPACING, "spacing axes must be finite positive numbers")

    chunk_value = _dig(raw, "chunk_size", "chunk_shape", "chunks")
    chunk_size = _as_int_tuple(chunk_value, 3, E_CHUNK, "chunk_size")
    if any(v <= 0 for v in chunk_size):
        raise WorkbenchError(E_CHUNK, "chunk_size must contain positive integers")
    chunks = chunk_grid(shape, chunk_size)
    covered = np.zeros(shape, dtype=bool)
    for chunk in chunks:
        b = chunk.core
        covered[b.z0:b.z1, b.y0:b.y1, b.x0:b.x1] = True
    if not covered.all():
        raise WorkbenchError(E_CHUNK, "chunk grid does not completely cover volume")

    rg = raw.get("region_growth")
    rg = rg if isinstance(rg, dict) else {}
    threshold_value = (
        _dig(rg, "threshold", "intensity_range", "range")
        or _dig(raw, "growth_threshold", "threshold", "intensity_range")
    )
    if not isinstance(threshold_value, (list, tuple)) or len(threshold_value) != 2:
        raise WorkbenchError(E_THRESHOLD, "region-growth threshold must be [lower, upper]")
    try:
        low = float(threshold_value[0])
        high = float(threshold_value[1])
    except (TypeError, ValueError) as exc:
        raise WorkbenchError(E_THRESHOLD, "threshold bounds must be numbers") from exc
    if not math.isfinite(low) or not math.isfinite(high) or low >= high:
        raise WorkbenchError(E_THRESHOLD, "threshold requires finite lower < upper")
    threshold = (low, high)

    stride_value = _dig(rg, "seed_stride", "seed_grid_stride") or raw.get("seed_stride") or [4, 8, 8]
    seed_stride = _as_int_tuple(stride_value, 3, E_SEED, "seed_stride")
    if any(v <= 0 for v in seed_stride):
        raise WorkbenchError(E_SEED, "seed_stride must contain positive integers")

    growth_radius = _dig(rg, "growth_radius", "max_growth_distance", "radius")
    if growth_radius is None:
        growth_radius = 2
    if not _is_int(growth_radius) or growth_radius < 0:
        raise WorkbenchError(E_SEED, "growth_radius must be a non-negative integer")

    morph_raw = raw.get("morphology")
    if morph_raw is None:
        morph_raw = {}
    if not isinstance(morph_raw, dict):
        raise WorkbenchError(E_MORPHOLOGY, "morphology must be an object")
    operations = _parse_morphology(morph_raw)

    parallelism = raw.get("parallelism", raw.get("workers", 1))
    if not _is_positive_int(parallelism):
        raise WorkbenchError(E_PARALLELISM, "parallelism must be a positive integer")

    time_budget = _dig(raw, "logic_time_budget_ms", "time_budget_ms", "logical_time_budget_ms")
    if time_budget is None:
        time_budget = 10_000.0
    if not _finite_positive_number(time_budget):
        raise WorkbenchError(E_TIME_BUDGET, "logic_time_budget_ms must be a finite positive number")

    memory_budget = _dig(raw, "pixel_memory_budget", "memory_budget_pixels", "memory_budget")
    if not _is_positive_int(memory_budget):
        raise WorkbenchError(E_MEMORY_BUDGET, "pixel_memory_budget must be a positive integer")

    seed = raw.get("seed", 0)
    if not _is_int(seed):
        raise WorkbenchError(E_TASK_SCHEMA, "seed must be an integer")

    base_dir = task_path.parent
    volume_dir = Path(raw.get("volume_dir", "volume"))
    if not volume_dir.is_absolute():
        volume_dir = base_dir / volume_dir
    truth_dir = Path(raw.get("truth_dir", "truth"))
    if not truth_dir.is_absolute():
        truth_dir = base_dir / truth_dir
    state_dir = Path(raw.get("state_dir", ".medseg_state"))
    if not state_dir.is_absolute():
        state_dir = base_dir / state_dir
    temp_dir = Path(raw.get("temp_dir", "tmp"))
    if not temp_dir.is_absolute():
        temp_dir = base_dir / temp_dir
    export_dir = Path(raw.get("export_dir", "exports"))
    if not export_dir.is_absolute():
        export_dir = base_dir / export_dir

    return TaskConfig(
        task_path=task_path,
        base_dir=base_dir,
        raw=raw,
        seed=int(seed),
        shape=tuple(shape),  # type: ignore[arg-type]
        spacing=tuple(spacing),
        chunk_size=tuple(chunk_size),  # type: ignore[arg-type]
        threshold=threshold,
        seed_stride=tuple(seed_stride),  # type: ignore[arg-type]
        growth_radius=int(growth_radius),
        morph_operations=tuple(operations),
        parallelism=int(parallelism),
        logic_time_budget_ms=float(time_budget),
        pixel_memory_budget=int(memory_budget),
        volume_dir=volume_dir,
        truth_dir=truth_dir,
        state_dir=state_dir,
        temp_dir=temp_dir,
        export_dir=export_dir,
        volume_pattern=str(raw.get("volume_pattern", "slice_{index:04d}.npy")),
    )


def _parse_morphology(morph_raw: Dict[str, Any]) -> List[MorphOperation]:
    if "operations" in morph_raw:
        ops_raw = morph_raw["operations"]
        if not isinstance(ops_raw, list) or not ops_raw:
            raise WorkbenchError(E_MORPHOLOGY, "morphology.operations must be a non-empty list")
        result: List[MorphOperation] = []
        for item in ops_raw:
            if not isinstance(item, dict):
                raise WorkbenchError(E_MORPHOLOGY, "morphology operation must be an object")
            op = str(item.get("op", "")).lower()
            if op not in {"dilate", "erode", "dilation", "erosion"}:
                raise WorkbenchError(E_MORPHOLOGY, "morphology op must be dilate or erode")
            op = "dilate" if op.startswith("dilat") else "erode"
            kernel = item.get("kernel", item.get("kernel_size", item.get("size")))
            iterations = item.get("iterations", 1)
            _validate_kernel(kernel)
            if not _is_positive_int(iterations):
                raise WorkbenchError(E_MORPHOLOGY, "morphology iterations must be a positive integer")
            result.append(MorphOperation(op, int(kernel), int(iterations)))
        return result

    common_kernel = morph_raw.get("kernel_size", morph_raw.get("kernel"))
    dilate_kernel = _dig(morph_raw, "dilate_kernel", "dilation_kernel", "dilate_kernel_size")
    erode_kernel = _dig(morph_raw, "erode_kernel", "erosion_kernel", "erode_kernel_size")
    if common_kernel is not None:
        if dilate_kernel is None:
            dilate_kernel = common_kernel
        if erode_kernel is None:
            erode_kernel = common_kernel
    if dilate_kernel is None:
        dilate_kernel = 3
    if erode_kernel is None:
        erode_kernel = 3
    _validate_kernel(dilate_kernel)
    _validate_kernel(erode_kernel)
    dilate_iterations = morph_raw.get("dilate_iterations", morph_raw.get("dilation_iterations", 1))
    erode_iterations = morph_raw.get("erode_iterations", morph_raw.get("erosion_iterations", 1))
    if not _is_positive_int(dilate_iterations) or not _is_positive_int(erode_iterations):
        raise WorkbenchError(E_MORPHOLOGY, "morphology iterations must be positive integers")
    order = str(morph_raw.get("order", "dilate_then_erode")).lower()
    dilate = MorphOperation("dilate", int(dilate_kernel), int(dilate_iterations))
    erode = MorphOperation("erode", int(erode_kernel), int(erode_iterations))
    if order in {"erode_then_dilate", "opening", "open"}:
        return [erode, dilate]
    return [dilate, erode]


def _validate_kernel(kernel: Any) -> None:
    if not _is_int(kernel) or kernel <= 0 or kernel % 2 != 1:
        raise WorkbenchError(E_MORPHOLOGY, "morphology kernel must be a positive odd integer")


def volume_paths(cfg: TaskConfig, directory: Optional[Path] = None) -> List[Path]:
    directory = Path(directory or cfg.volume_dir)
    expected = [
        directory / cfg.volume_pattern.format(index=index, i=index)
        for index in range(cfg.shape[0])
    ]
    if all(path.is_file() for path in expected):
        return expected
    arrays = sorted(directory.glob("*.npy")) if directory.is_dir() else []
    if len(arrays) == cfg.shape[0]:
        return arrays
    missing = [str(path.name) for path in expected if not path.is_file()][:5]
    raise WorkbenchError(
        E_VOLUME,
        f"volume slice directory is invalid: expected {cfg.shape[0]} integer 2D slices",
        details={"directory": str(directory), "missing_example": missing, "found_npy": len(arrays)},
    )


def read_stack(cfg: TaskConfig, *, truth: bool = False) -> Tuple[np.ndarray, str]:
    directory = cfg.truth_dir if truth else cfg.volume_dir
    paths = volume_paths(cfg, directory)
    slices: List[np.ndarray] = []
    digests: List[str] = []
    for path in paths:
        try:
            arr = np.load(path, allow_pickle=False)
        except Exception as exc:
            code = "E_TRUTH" if truth else E_VOLUME
            raise WorkbenchError(code, f"cannot load slice {path.name}: {exc}") from exc
        if arr.ndim != 2:
            code = "E_TRUTH" if truth else E_VOLUME
            raise WorkbenchError(code, f"slice {path.name} must be a 2D array")
        if arr.dtype.kind not in {"i", "u"}:
            code = "E_TRUTH" if truth else E_VOLUME
            raise WorkbenchError(code, f"slice {path.name} must have an integer dtype")
        if tuple(arr.shape) != (cfg.shape[1], cfg.shape[2]):
            code = "E_DIMENSION"
            raise WorkbenchError(
                code,
                f"slice {path.name} has shape {arr.shape}, expected {(cfg.shape[1], cfg.shape[2])}",
            )
        slices.append(np.asarray(arr))
        digests.append(f"{path.name}:{_array_content_digest(arr)}")
    stack = np.stack(slices, axis=0).astype(np.int64, copy=False)
    digest = canonical_digest(
        {"shape": list(stack.shape), "slices": digests, "truth": truth}
    )
    return stack, digest


def _array_content_digest(arr: np.ndarray) -> str:
    from .io_utils import array_digest

    return array_digest(arr)


def compute_version_id(cfg: TaskConfig, volume_digest: str) -> str:
    return canonical_digest(
        {"volume_digest": volume_digest, "parameters": cfg.normalized_parameters()}
    )[:16]
