from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import WorkbenchError

CONFIG_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class Octave:
    frequency: float
    amplitude: float


@dataclass(frozen=True)
class WorkbenchConfig:
    width: int
    height: int
    grid_rows: int
    grid_cols: int
    seed: int
    octaves: tuple[Octave, ...]
    light_direction: tuple[float, float, float]
    relief_strength: float
    normalization_tolerance: float
    concurrency: int
    memory_budget_bytes: int
    preview_tile: tuple[int, int] = (0, 0)

    @property
    def tile_width(self) -> int:
        return self.width // self.grid_cols

    @property
    def tile_height(self) -> int:
        return self.height // self.grid_rows

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "schema_version": CONFIG_SCHEMA_VERSION,
            "canvas": {"width": self.width, "height": self.height},
            "grid": {"rows": self.grid_rows, "cols": self.grid_cols},
            "seed": self.seed,
            "octaves": [
                {
                    "frequency": float.hex(octave.frequency),
                    "amplitude": float.hex(octave.amplitude),
                }
                for octave in self.octaves
            ],
            "light_direction": [float.hex(value) for value in self.light_direction],
            "relief_strength": float.hex(self.relief_strength),
            "normalization_tolerance": float.hex(self.normalization_tolerance),
            "concurrency": self.concurrency,
            "memory_budget_bytes": self.memory_budget_bytes,
            "preview_tile": list(self.preview_tile),
        }

    @property
    def version(self) -> str:
        payload = json.dumps(
            self.canonical_payload(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def public_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "grid_rows": self.grid_rows,
            "grid_cols": self.grid_cols,
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "seed": self.seed,
            "octaves": [
                {"frequency": octave.frequency, "amplitude": octave.amplitude}
                for octave in self.octaves
            ],
            "light_direction": list(self.light_direction),
            "relief_strength": self.relief_strength,
            "normalization_tolerance": self.normalization_tolerance,
            "concurrency": self.concurrency,
            "memory_budget_bytes": self.memory_budget_bytes,
            "preview_tile": list(self.preview_tile),
        }


def _error(code: str, message: str, **details: Any) -> WorkbenchError:
    return WorkbenchError(code, message, details)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _require_int(
    value: Any,
    field: str,
    *,
    minimum: int = 0,
    code: str,
) -> int:
    if not _is_int(value):
        raise _error(code, f"{field} must be an integer", field=field, value=value)
    if value < minimum:
        raise _error(
            code,
            f"{field} must be >= {minimum}",
            field=field,
            value=value,
        )
    return int(value)


def _require_finite_positive(value: Any, field: str, code: str) -> float:
    if not _is_number(value) or float(value) <= 0.0:
        raise _error(
            code,
            f"{field} must be a finite positive number",
            field=field,
            value=value,
        )
    return float(value)


def _mapping(value: Any, field: str, code: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error(code, f"{field} must be an object", field=field)
    return value


def _resolve_canvas(data: dict[str, Any]) -> dict[str, Any]:
    if "canvas" in data:
        return _mapping(data["canvas"], "canvas", "E_CANVAS")
    return data


def _resolve_grid(data: dict[str, Any]) -> dict[str, Any]:
    if "grid" in data:
        return _mapping(data["grid"], "grid", "E_GRID")
    return data


def _resolve_light(data: dict[str, Any]) -> tuple[list[Any], Any]:
    light = data.get("light")
    if light is None:
        direction = data.get("light_direction", data.get("direction"))
        strength = data.get("relief_strength")
        return direction, strength
    light_map = _mapping(light, "light", "E_LIGHT")
    direction = light_map.get("direction", light_map.get("vector"))
    strength = light_map.get("relief_strength", data.get("relief_strength"))
    return direction, strength


def config_from_dict(raw: Any) -> WorkbenchConfig:
    if not isinstance(raw, dict):
        raise _error("E_CONFIG_SHAPE", "configuration root must be a JSON object")

    canvas = _resolve_canvas(raw)
    grid = _resolve_grid(raw)

    width = _require_int(
        canvas.get("width"), "canvas.width", minimum=1, code="E_CANVAS"
    )
    height = _require_int(
        canvas.get("height"), "canvas.height", minimum=1, code="E_CANVAS"
    )
    rows = _require_int(
        grid.get("rows", grid.get("grid_rows")),
        "grid.rows",
        minimum=1,
        code="E_GRID",
    )
    cols = _require_int(
        grid.get("cols", grid.get("grid_cols")),
        "grid.cols",
        minimum=1,
        code="E_GRID",
    )
    if width % cols != 0:
        raise _error(
            "E_GRID_DIVISIBILITY",
            "canvas width must be divisible by grid columns",
            width=width,
            grid_cols=cols,
        )
    if height % rows != 0:
        raise _error(
            "E_GRID_DIVISIBILITY",
            "canvas height must be divisible by grid rows",
            height=height,
            grid_rows=rows,
        )

    seed = _require_int(raw.get("seed"), "seed", minimum=0, code="E_SEED")

    octaves_raw = raw.get("octaves")
    if not isinstance(octaves_raw, list) or not octaves_raw:
        raise _error(
            "E_OCTAVES",
            "octaves must be a non-empty array",
            field="octaves",
        )
    octaves: list[Octave] = []
    previous_frequency = -math.inf
    for index, item in enumerate(octaves_raw):
        item_map = _mapping(item, f"octaves[{index}]", "E_OCTAVES")
        frequency = _require_finite_positive(
            item_map.get("frequency"),
            f"octaves[{index}].frequency",
            "E_FREQUENCY",
        )
        amplitude = _require_finite_positive(
            item_map.get("amplitude"),
            f"octaves[{index}].amplitude",
            "E_AMPLITUDE",
        )
        if frequency <= previous_frequency:
            raise _error(
                "E_FREQUENCY_ORDER",
                "octave frequencies must be strictly increasing",
                index=index,
                previous=previous_frequency,
                current=frequency,
            )
        previous_frequency = frequency
        octaves.append(Octave(frequency=frequency, amplitude=amplitude))

    direction_raw, strength_raw = _resolve_light(raw)
    if not isinstance(direction_raw, (list, tuple)) or len(direction_raw) != 3:
        raise _error(
            "E_LIGHT_VECTOR",
            "light direction must contain exactly three finite numbers",
            value=direction_raw,
        )
    direction: list[float] = []
    for index, value in enumerate(direction_raw):
        if not _is_number(value):
            raise _error(
                "E_LIGHT_VECTOR",
                "light direction must contain exactly three finite numbers",
                index=index,
                value=value,
            )
        direction.append(float(value))
    norm = math.sqrt(sum(value * value for value in direction))
    if abs(norm - 1.0) > 1e-8:
        raise _error(
            "E_LIGHT_UNIT",
            "light direction must be a unit vector",
            norm=norm,
        )

    relief_strength = _require_finite_positive(
        strength_raw, "relief_strength", "E_RELIEF_STRENGTH"
    )
    tolerance = _require_finite_positive(
        raw.get("normalization_tolerance", raw.get("tolerance")),
        "normalization_tolerance",
        "E_TOLERANCE",
    )
    concurrency = _require_int(
        raw.get("concurrency", raw.get("max_workers")),
        "concurrency",
        minimum=1,
        code="E_CONCURRENCY",
    )

    if "memory_budget_bytes" in raw:
        memory_budget_bytes = _require_int(
            raw.get("memory_budget_bytes"),
            "memory_budget_bytes",
            minimum=1,
            code="E_MEMORY_BUDGET",
        )
    else:
        memory_budget_mb = _require_finite_positive(
            raw.get("memory_budget_mb"),
            "memory_budget_mb",
            "E_MEMORY_BUDGET",
        )
        memory_budget_bytes = max(1, int(memory_budget_mb * 1024 * 1024))

    preview_raw = raw.get("preview_tile", [0, 0])
    if (
        not isinstance(preview_raw, (list, tuple))
        or len(preview_raw) != 2
        or not all(_is_int(value) for value in preview_raw)
    ):
        raise _error(
            "E_PREVIEW_TILE",
            "preview_tile must be [row, col] integers",
            value=preview_raw,
        )
    preview_row, preview_col = int(preview_raw[0]), int(preview_raw[1])
    if not (0 <= preview_row < rows and 0 <= preview_col < cols):
        raise _error(
            "E_PREVIEW_TILE",
            "preview_tile is outside the configured grid",
            row=preview_row,
            col=preview_col,
            grid_rows=rows,
            grid_cols=cols,
        )

    return WorkbenchConfig(
        width=width,
        height=height,
        grid_rows=rows,
        grid_cols=cols,
        seed=seed,
        octaves=tuple(octaves),
        light_direction=(direction[0], direction[1], direction[2]),
        relief_strength=relief_strength,
        normalization_tolerance=tolerance,
        concurrency=concurrency,
        memory_budget_bytes=memory_budget_bytes,
        preview_tile=(preview_row, preview_col),
    )


def load_config(path: str | Path) -> WorkbenchConfig:
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise _error(
            "E_CONFIG_IO",
            "unable to read configuration file",
            path=str(config_path),
            reason=str(exc),
        ) from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise _error(
            "E_CONFIG_JSON",
            "configuration is not valid JSON",
            path=str(config_path),
            line=exc.lineno,
            column=exc.colno,
        ) from exc
    try:
        return config_from_dict(raw)
    except WorkbenchError as exc:
        exc.details.setdefault("path", str(config_path))
        raise


def sample_config() -> WorkbenchConfig:
    """Return the fixed local acceptance sample."""

    return WorkbenchConfig(
        width=512,
        height=384,
        grid_rows=4,
        grid_cols=4,
        seed=20261125,
        octaves=(
            Octave(1.0, 1.0),
            Octave(2.5, 0.5),
            Octave(5.75, 0.25),
            Octave(12.0, 0.125),
        ),
        light_direction=(0.48, -0.64, 0.6),
        relief_strength=2.4,
        normalization_tolerance=1e-6,
        concurrency=4,
        memory_budget_bytes=64 * 1024 * 1024,
        preview_tile=(0, 0),
    )


def sample_config_dict() -> dict[str, Any]:
    config = sample_config()
    return {
        "canvas": {"width": config.width, "height": config.height},
        "grid": {"rows": config.grid_rows, "cols": config.grid_cols},
        "seed": config.seed,
        "octaves": [
            {"frequency": octave.frequency, "amplitude": octave.amplitude}
            for octave in config.octaves
        ],
        "light": {
            "direction": list(config.light_direction),
            "relief_strength": config.relief_strength,
        },
        "normalization_tolerance": config.normalization_tolerance,
        "concurrency": config.concurrency,
        "memory_budget_mb": config.memory_budget_bytes / (1024 * 1024),
        "preview_tile": list(config.preview_tile),
    }
