"""Scene loading and validation for the grid-contour tool.

The scene JSON schema (all keys required unless noted):

{
  "grid": {
    "width":  <int >= 1>,
    "height": <int >= 1>,
    "values": [<number> | null, ...]   # width*height entries, row-major
  },
  "levels": [<number>, ...],
  "queries": [{"x": <number>, "y": <number>}, ...]   (optional, default [])
  "canvas": {"width": <int >= 1>, "height": <int >= 1>},
  "world": {"min_x": <number>, "min_y": <number>,
            "max_x": <number>, "max_y": <number>},     (optional)
  "background": [<r>, <g>, <b>],                       (optional)
  "line_width": <int >= 1>                             (optional)
}

Grid "values" are floats or null.  null, NaN and +/-Infinity are all treated as
invalid cells ("gaps"): a sample touching a gap produces no segments.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from typing import Any, Sequence

from .errors import SceneError

MIN_CANVAS = 1
MAX_CANVAS = 10000
MAX_CELLS = 5_000_000


def _is_number(value: Any) -> bool:
    # bool is an int subclass; JSON true/false must not pass as coordinates.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _is_finite_number(value: Any) -> bool:
    return _is_number(value) and math.isfinite(value)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Grid:
    width: int
    height: int
    values: tuple[float | None, ...]   # None marks a non-finite / missing cell

    def value(self, ix: int, iy: int) -> float | None:
        return self.values[iy * self.width + ix]


@dataclass(frozen=True)
class Bounds:
    min_x: float
    min_y: float
    max_x: float
    max_y: float

    @property
    def span_x(self) -> float:
        return self.max_x - self.min_x

    @property
    def span_y(self) -> float:
        return self.max_y - self.min_y


@dataclass(frozen=True)
class Canvas:
    width: int
    height: int


@dataclass(frozen=True)
class Scene:
    grid: Grid
    levels: tuple[float, ...]
    queries: tuple[tuple[float, float] | None, ...]  # None = unparseable entry
    raw_queries: tuple[dict[str, Any], ...]
    canvas: Canvas
    world: Bounds
    background: tuple[int, int, int] = (255, 255, 255)
    line_width: int = 2

    # ---- derived geometry ---------------------------------------------------

    @property
    def finite_values(self) -> list[float]:
        return [v for v in self.grid.values if v is not None]

    @property
    def data_min(self) -> float | None:
        vals = self.finite_values
        return min(vals) if vals else None

    @property
    def data_max(self) -> float | None:
        vals = self.finite_values
        return max(vals) if vals else None

    @property
    def cell_x(self) -> float:
        """World units per grid cell along x."""
        return self.world.span_x / (self.grid.width - 1) if self.grid.width > 1 else self.world.span_x

    @property
    def cell_y(self) -> float:
        return self.world.span_y / (self.grid.height - 1) if self.grid.height > 1 else self.world.span_y

    def world_to_pixel(self, wx: float, wy: float) -> tuple[float, float]:
        """World point -> pixel coordinates (continuous, not rounded)."""
        px = (wx - self.world.min_x) / self.world.span_x * self.canvas.width if self.world.span_x else 0.0
        py = (wy - self.world.min_y) / self.world.span_y * self.canvas.height if self.world.span_y else 0.0
        return px, py

    def in_bounds(self, wx: float, wy: float) -> bool:
        return (
            math.isfinite(wx)
            and math.isfinite(wy)
            and self.world.min_x <= wx <= self.world.max_x
            and self.world.min_y <= wy <= self.world.max_y
        )


@dataclass
class LoadedScene:
    scene: Scene
    affected_levels: tuple[float, ...] = ()  # levels touching invalid cells
    warnings: list[str] = field(default_factory=list)


def _require(obj: dict[str, Any], key: str, where: str) -> Any:
    if key not in obj:
        raise SceneError(f"缺少字段 {where}.{key}")
    return obj[key]


def _bytes(obj: Any, where: str) -> list[Any]:
    if not isinstance(obj, list) or len(obj) != 3:
        raise SceneError(f"字段 {where} 必须是长度为 3 的数字数组")
    for item in obj:
        if not _is_number(item) or not 0 <= item <= 255:
            raise SceneError(f"字段 {where} 的每个分量必须是 0-255 的数字")
    return obj


def parse_scene(data: Any) -> LoadedScene:
    """Validate a decoded scene dict and build a Scene.

    Raises SceneError with a Chinese, business-readable message on any
    structural problem.  Non-finite scalar values are *not* a structural
    problem; they are recorded as gaps and reported via affected levels.
    """
    if not isinstance(data, dict):
        raise SceneError("场景文件顶层必须是一个 JSON 对象")

    grid_raw = _require(data, "grid", "场景")
    if not isinstance(grid_raw, dict):
        raise SceneError("字段 场景.grid 必须是对象")
    width = _require(grid_raw, "width", "场景.grid")
    height = _require(grid_raw, "height", "场景.grid")
    if not _is_int(width) or width < 1:
        raise SceneError("字段 场景.grid.width 必须是正整数")
    if not _is_int(height) or height < 1:
        raise SceneError("字段 场景.grid.height 必须是正整数")
    if width * height > MAX_CELLS:
        raise SceneError(f"网格过大（{width}×{height}），最多支持 {MAX_CELLS} 个格点")
    values = _require(grid_raw, "values", "场景.grid")
    if not isinstance(values, list):
        raise SceneError("字段 场景.grid.values 必须是数字数组")
    expected = width * height
    if len(values) != expected:
        raise SceneError(
            f"标量数组长度不符：网格 {width}×{height} 需要 {expected} 个元素，"
            f"实际为 {len(values)} 个"
        )
    for index, raw in enumerate(values):
        if raw is not None and not _is_number(raw):
            raise SceneError(f"标量数组第 {index} 个元素（{raw!r}）不是数字")

    gaps = 0
    clean: list[float | None] = []
    for raw in values:
        if raw is None or not math.isfinite(raw):
            clean.append(None)
            gaps += 1
        else:
            clean.append(float(raw))

    canvas_raw = _require(data, "canvas", "场景")
    if not isinstance(canvas_raw, dict):
        raise SceneError("字段 场景.canvas 必须是对象")
    canvas_w = _require(canvas_raw, "width", "场景.canvas")
    canvas_h = _require(canvas_raw, "height", "场景.canvas")
    if not _is_int(canvas_w) or not MIN_CANVAS <= canvas_w <= MAX_CANVAS:
        raise SceneError(f"字段 场景.canvas.width 必须是 {MIN_CANVAS}-{MAX_CANVAS} 的整数")
    if not _is_int(canvas_h) or not MIN_CANVAS <= canvas_h <= MAX_CANVAS:
        raise SceneError(f"字段 场景.canvas.height 必须是 {MIN_CANVAS}-{MAX_CANVAS} 的整数")

    levels_raw = _require(data, "levels", "场景")
    if not isinstance(levels_raw, list):
        raise SceneError("字段 场景.levels 必须是数字数组")
    if not levels_raw:
        raise SceneError("字段 场景.levels 不能为空")
    levels: list[float] = []
    for index, raw in enumerate(levels_raw):
        if not _is_finite_number(raw):
            raise SceneError(f"字段 场景.levels 第 {index} 项（{raw!r}）必须是有限数字")
        levels.append(float(raw))
    # Duplicated levels collapse to a single entry; output order is ascending.
    levels_sorted = tuple(sorted(set(levels)))

    queries_raw = data.get("queries", [])
    if not isinstance(queries_raw, list):
        raise SceneError("字段 场景.queries 必须是数组")
    queries: list[tuple[float, float] | None] = []
    raw_queries: list[dict[str, Any]] = []
    warnings: list[str] = []
    for index, raw in enumerate(queries_raw):
        if not isinstance(raw, dict):
            queries.append(None)
            raw_queries.append({"index": index})
            warnings.append(f"查询点 {index}：条目不是对象，已跳过")
            continue
        raw_queries.append(dict(raw))
        x, y = raw.get("x"), raw.get("y")
        if not (_is_finite_number(x) and _is_finite_number(y)):
            queries.append(None)
            warnings.append(f"查询点 {index}：坐标缺失或不是有限数字，已跳过")
            continue
        queries.append((float(x), float(y)))

    world_raw = data.get("world")
    if world_raw is None:
        world = Bounds(0.0, 0.0, float(max(width - 1, 1)), float(max(height - 1, 1)))
    else:
        if not isinstance(world_raw, dict):
            raise SceneError("字段 场景.world 必须是对象")
        keys = ("min_x", "min_y", "max_x", "max_y")
        coords = []
        for key in keys:
            value = _require(world_raw, key, "场景.world")
            if not _is_finite_number(value):
                raise SceneError(f"字段 场景.world.{key} 必须是有限数字")
            coords.append(float(value))
        min_x, min_y, max_x, max_y = coords
        if max_x <= min_x or max_y <= min_y:
            raise SceneError("字段 场景.world 的 max_x/max_y 必须大于 min_x/min_y")
        world = Bounds(min_x, min_y, max_x, max_y)

    background = (255, 255, 255)
    if "background" in data:
        rgb = _bytes(data["background"], "场景.background")
        background = (int(rgb[0]), int(rgb[1]), int(rgb[2]))

    line_width = data.get("line_width", 2)
    if not _is_int(line_width) or line_width < 1:
        raise SceneError("字段 场景.line_width 必须是正整数")

    grid = Grid(width, height, tuple(clean))
    scene = Scene(
        grid=grid,
        levels=levels_sorted,
        queries=tuple(queries),
        raw_queries=tuple(raw_queries),
        canvas=Canvas(canvas_w, canvas_h),
        world=world,
        background=background,
        line_width=line_width,
    )
    affected = affected_levels(grid, levels_sorted)

    if gaps:
        warnings.append(f"标量数组含 {gaps} 个无效值（NaN/Infinity/null），相关格边不参与等值线提取")
    return LoadedScene(scene=scene, affected_levels=affected, warnings=warnings)


def affected_levels(grid: Grid, levels: Sequence[float]) -> tuple[float, ...]:
    """Levels whose extraction is incomplete because of a gap cell.

    Any cell containing a non-finite sample is skipped entirely, so a level
    that could have crossed one of its (finite) edges is unaccounted for.  A
    level is reported when it lies inside the value range of the finite
    samples sharing a cell with a gap.
    """
    hits: list[float] = []
    w, h = grid.width, grid.height
    for iy in range(max(h - 1, 1)):
        for ix in range(max(w - 1, 1)):
            corners = [
                grid.value(ix, iy),
                grid.value(min(ix + 1, w - 1), iy),
                grid.value(min(ix + 1, w - 1), min(iy + 1, h - 1)),
                grid.value(ix, min(iy + 1, h - 1)),
            ]
            if all(value is not None for value in corners):
                continue  # fully usable cell
            finite = [value for value in corners if value is not None]
            if len(finite) < 2:
                continue
            lo, hi = min(finite), max(finite)
            for level in levels:
                if lo <= level <= hi and level not in hits:
                    hits.append(level)
    return tuple(sorted(hits))


def load_scene(path: str) -> LoadedScene:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raise SceneError(f"场景文件不存在：{path}")
    except OSError as exc:
        raise SceneError(f"无法读取场景文件 {path}：{exc}")
    except json.JSONDecodeError as exc:
        raise SceneError(f"场景文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）：{exc.msg}")
    except UnicodeDecodeError:
        raise SceneError(f"场景文件 {path} 不是 UTF-8 编码的文本")
    except ValueError as exc:
        # json's parse_constant path or a NaN literal outside of a list context.
        raise SceneError(f"场景文件包含无法解析的数值：{exc}")
    return parse_scene(raw)
