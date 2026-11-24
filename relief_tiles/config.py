"""Configuration parsing, validation and canonical version identity."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from .errors import BusinessError


SCHEMA_VERSION = 1
DEFAULT_GRADIENT_TABLE_SIZE = 256


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _require_object(value: Any, code: str, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise BusinessError(code, f"{field_name} must be a JSON object")
    return value


def _require_int(
    value: Any, code: str, field_name: str, minimum: int | None = None
) -> int:
    if not _is_int(value):
        raise BusinessError(code, f"{field_name} must be an integer")
    if minimum is not None and value < minimum:
        raise BusinessError(code, f"{field_name} must be >= {minimum}")
    return int(value)


def _require_number(
    value: Any, code: str, field_name: str, minimum: float | None = None
) -> float:
    if not _is_number(value):
        raise BusinessError(code, f"{field_name} must be a finite number")
    result = float(value)
    if minimum is not None and result < minimum:
        raise BusinessError(code, f"{field_name} must be >= {minimum}")
    return result


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Octave:
    frequency: float
    amplitude: float


@dataclass(frozen=True)
class Camera:
    azimuth_deg: float
    elevation_deg: float
    distance: float


@dataclass(frozen=True)
class ReliefConfig:
    width: int
    height: int
    rows: int
    cols: int
    seed: int
    octaves: tuple[Octave, ...]
    light_direction: tuple[float, float, float]
    emboss_strength: float
    normalization_min: int
    normalization_max: int
    quantization_levels: int
    camera: Camera
    preview_row: int
    preview_col: int
    concurrency_limit: int
    memory_budget_pixels: int
    gradient_table_size: int = DEFAULT_GRADIENT_TABLE_SIZE

    @property
    def block_width(self) -> int:
        return self.width // self.cols

    @property
    def block_height(self) -> int:
        return self.height // self.rows

    @property
    def block_count(self) -> int:
        return self.rows * self.cols

    @property
    def flight_pixels_per_block(self) -> int:
        return (self.block_width + 2) * (self.block_height + 2)

    @property
    def version(self) -> str:
        return sha256_json(self.normalized())

    @property
    def normalization_span(self) -> int:
        return self.normalization_max - self.normalization_min

    def normalized(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "canvas": {"width": self.width, "height": self.height},
            "grid": {"rows": self.rows, "cols": self.cols},
            "seed": self.seed,
            "pattern": {
                "octaves": [
                    {"frequency": octave.frequency, "amplitude": octave.amplitude}
                    for octave in self.octaves
                ],
                "gradient_table_size": self.gradient_table_size,
            },
            "light": {"direction": list(self.light_direction)},
            "emboss": {
                "strength": self.emboss_strength,
                "normalization_min": self.normalization_min,
                "normalization_max": self.normalization_max,
                "quantization_levels": self.quantization_levels,
            },
            "camera": {
                "azimuth_deg": self.camera.azimuth_deg,
                "elevation_deg": self.camera.elevation_deg,
                "distance": self.camera.distance,
            },
            "slice": {
                "preview_row": self.preview_row,
                "preview_col": self.preview_col,
            },
            "resources": {
                "concurrency_limit": self.concurrency_limit,
                "memory_budget_pixels": self.memory_budget_pixels,
            },
        }

    def block_bounds(self, row: int, col: int) -> tuple[int, int, int, int]:
        if not _is_int(row) or not _is_int(col):
            raise BusinessError("E_BLOCK_RANGE", "row and col must be integers")
        if row < 0 or row >= self.rows or col < 0 or col >= self.cols:
            raise BusinessError(
                "E_BLOCK_RANGE",
                f"block ({row}, {col}) is outside the {self.rows}x{self.cols} grid",
            )
        x0 = col * self.block_width
        y0 = row * self.block_height
        return x0, y0, self.block_width, self.block_height

    def iter_block_indices(self) -> Iterable[tuple[int, int]]:
        for row in range(self.rows):
            for col in range(self.cols):
                yield row, col


def _parse_config(data: Any) -> ReliefConfig:
    root = _require_object(data, "E_CONFIG", "config")
    schema_version = root.get("schema_version", SCHEMA_VERSION)
    if not _is_int(schema_version) or schema_version != SCHEMA_VERSION:
        raise BusinessError(
            "E_CONFIG_VERSION",
            f"schema_version must be {SCHEMA_VERSION}",
            {"schema_version": schema_version},
        )

    canvas = _require_object(root.get("canvas"), "E_CANVAS", "canvas")
    width = _require_int(canvas.get("width"), "E_CANVAS", "canvas.width", 1)
    height = _require_int(canvas.get("height"), "E_CANVAS", "canvas.height", 1)

    grid = _require_object(root.get("grid"), "E_GRID", "grid")
    rows = _require_int(grid.get("rows"), "E_GRID", "grid.rows", 1)
    cols = _require_int(grid.get("cols"), "E_GRID", "grid.cols", 1)
    if width % cols != 0 or height % rows != 0:
        raise BusinessError(
            "E_GRID",
            "canvas width/height must be exactly divisible by grid cols/rows",
            {
                "width": width,
                "height": height,
                "rows": rows,
                "cols": cols,
            },
        )

    seed = _require_int(root.get("seed"), "E_SEED", "seed", 0)
    if seed > 0xFFFFFFFFFFFFFFFF:
        raise BusinessError("E_SEED", "seed must fit in an unsigned 64-bit integer")

    pattern = _require_object(root.get("pattern"), "E_OCTAVE", "pattern")
    octave_values = pattern.get("octaves")
    if not isinstance(octave_values, list) or not octave_values:
        raise BusinessError("E_OCTAVE", "pattern.octaves must be a non-empty array")
    octaves: list[Octave] = []
    previous_frequency: float | None = None
    for index, item in enumerate(octave_values):
        entry = _require_object(item, "E_OCTAVE", f"pattern.octaves[{index}]")
        frequency = _require_number(
            entry.get("frequency"),
            "E_FREQUENCY",
            f"pattern.octaves[{index}].frequency",
        )
        amplitude = _require_number(
            entry.get("amplitude"),
            "E_AMPLITUDE",
            f"pattern.octaves[{index}].amplitude",
        )
        if frequency <= 0:
            raise BusinessError(
                "E_FREQUENCY",
                f"pattern.octaves[{index}].frequency must be > 0",
            )
        if amplitude <= 0:
            raise BusinessError(
                "E_AMPLITUDE",
                f"pattern.octaves[{index}].amplitude must be > 0",
            )
        if previous_frequency is not None and frequency <= previous_frequency:
            raise BusinessError(
                "E_FREQUENCY",
                "octave frequencies must be strictly increasing",
                {"index": index, "previous": previous_frequency, "current": frequency},
            )
        previous_frequency = frequency
        octaves.append(Octave(frequency, amplitude))

    gradient_table_size = _require_int(
        pattern.get("gradient_table_size", DEFAULT_GRADIENT_TABLE_SIZE),
        "E_OCTAVE",
        "pattern.gradient_table_size",
        8,
    )
    if gradient_table_size & (gradient_table_size - 1):
        raise BusinessError(
            "E_OCTAVE", "pattern.gradient_table_size must be a power of two"
        )
    if gradient_table_size > 65536:
        raise BusinessError(
            "E_OCTAVE", "pattern.gradient_table_size must be <= 65536"
        )

    light = _require_object(root.get("light"), "E_LIGHT", "light")
    direction_value = light.get("direction")
    if not isinstance(direction_value, list) or len(direction_value) != 3:
        raise BusinessError(
            "E_LIGHT", "light.direction must contain exactly three numbers"
        )
    direction = tuple(
        _require_number(value, "E_LIGHT", f"light.direction[{index}]")
        for index, value in enumerate(direction_value)
    )
    norm = math.sqrt(sum(component * component for component in direction))
    if abs(norm - 1.0) > 1e-6:
        raise BusinessError(
            "E_LIGHT",
            "light.direction must be a unit vector",
            {"norm": norm},
        )

    emboss = _require_object(root.get("emboss"), "E_EMBOSS", "emboss")
    emboss_strength = _require_number(
        emboss.get("strength"), "E_EMBOSS", "emboss.strength"
    )
    if emboss_strength <= 0:
        raise BusinessError("E_EMBOSS", "emboss.strength must be > 0")

    normalization_min = _require_int(
        emboss.get("normalization_min"),
        "E_NORMALIZATION",
        "emboss.normalization_min",
    )
    normalization_max = _require_int(
        emboss.get("normalization_max"),
        "E_NORMALIZATION",
        "emboss.normalization_max",
    )
    if normalization_max <= normalization_min:
        raise BusinessError(
            "E_NORMALIZATION",
            "normalization_max - normalization_min must be > 0",
        )
    if normalization_min < 0 or normalization_max > 65535:
        raise BusinessError(
            "E_NORMALIZATION",
            "normalization range must fit in unsigned 16-bit grayscale",
        )

    quantization_levels = _require_int(
        emboss.get("quantization_levels"),
        "E_QUANTIZATION",
        "emboss.quantization_levels",
        2,
    )
    if quantization_levels > 65536:
        raise BusinessError(
            "E_QUANTIZATION", "emboss.quantization_levels must be <= 65536"
        )
    if quantization_levels - 1 > normalization_max - normalization_min:
        raise BusinessError(
            "E_QUANTIZATION",
            "normalization range cannot represent the declared quantization levels",
        )

    camera_value = _require_object(root.get("camera"), "E_CAMERA", "camera")
    camera = Camera(
        azimuth_deg=_require_number(
            camera_value.get("azimuth_deg"),
            "E_CAMERA",
            "camera.azimuth_deg",
        ),
        elevation_deg=_require_number(
            camera_value.get("elevation_deg"),
            "E_CAMERA",
            "camera.elevation_deg",
        ),
        distance=_require_number(
            camera_value.get("distance"),
            "E_CAMERA",
            "camera.distance",
        ),
    )
    if camera.elevation_deg < -90.0 or camera.elevation_deg > 90.0:
        raise BusinessError(
            "E_CAMERA", "camera.elevation_deg must be within [-90, 90]"
        )
    if camera.distance <= 0:
        raise BusinessError("E_CAMERA", "camera.distance must be > 0")

    slice_value = _require_object(root.get("slice", {}), "E_BLOCK_RANGE", "slice")
    preview_row = _require_int(
        slice_value.get("preview_row", 0), "E_BLOCK_RANGE", "slice.preview_row", 0
    )
    preview_col = _require_int(
        slice_value.get("preview_col", 0), "E_BLOCK_RANGE", "slice.preview_col", 0
    )
    if preview_row >= rows or preview_col >= cols:
        raise BusinessError(
            "E_BLOCK_RANGE",
            "preview block is outside the configured grid",
            {"row": preview_row, "col": preview_col, "rows": rows, "cols": cols},
        )

    resources = _require_object(root.get("resources"), "E_RESOURCE", "resources")
    concurrency_limit = _require_int(
        resources.get("concurrency_limit"),
        "E_RESOURCE",
        "resources.concurrency_limit",
        1,
    )
    memory_budget_pixels = _require_int(
        resources.get("memory_budget_pixels"),
        "E_RESOURCE",
        "resources.memory_budget_pixels",
        1,
    )
    flight_pixels = ((width // cols) + 2) * ((height // rows) + 2)
    if memory_budget_pixels < flight_pixels:
        raise BusinessError(
            "E_BUDGET",
            "memory_budget_pixels is smaller than one block working set",
            {
                "memory_budget_pixels": memory_budget_pixels,
                "block_working_pixels": flight_pixels,
            },
        )

    return ReliefConfig(
        width=width,
        height=height,
        rows=rows,
        cols=cols,
        seed=seed,
        octaves=tuple(octaves),
        light_direction=direction,
        emboss_strength=emboss_strength,
        normalization_min=normalization_min,
        normalization_max=normalization_max,
        quantization_levels=quantization_levels,
        camera=camera,
        preview_row=preview_row,
        preview_col=preview_col,
        concurrency_limit=concurrency_limit,
        memory_budget_pixels=memory_budget_pixels,
        gradient_table_size=gradient_table_size,
    )


def parse_config(data: Any) -> ReliefConfig:
    return _parse_config(data)


def load_config(path: str | Path) -> ReliefConfig:
    config_path = Path(path)
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise BusinessError(
            "E_CONFIG_IO", "config file does not exist", {"path": str(config_path)}
        ) from exc
    except OSError as exc:
        raise BusinessError(
            "E_CONFIG_IO",
            "failed to read config file",
            {"path": str(config_path), "error": str(exc)},
        ) from exc
    except json.JSONDecodeError as exc:
        raise BusinessError(
            "E_CONFIG_JSON",
            "config file is not valid JSON",
            {"path": str(config_path), "line": exc.lineno, "column": exc.colno},
        ) from exc
    return _parse_config(data)
