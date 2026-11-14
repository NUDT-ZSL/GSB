"""配置加载与校验。

配置 JSON 结构::

    {
      "sheet":  {"width": 96, "height": 96},
      "grid":   {"rows": 4, "columns": 4},
      "patches": [
        {"row": 0, "column": 1, "path": "p1.png", "offset_x": 0, "offset_y": 2.6},
        {"index": 6, "path": "p2.png"}
      ]
    }

所有校验错误都会被收集，一次性报告（JSON 指针风格定位，如
``/grid/rows``、``/patches/2/path``），任一字段非法即抛出
``ConfigError``，调用方不得产出任何输出文件。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import ConfigError

_TOP_KEYS = {"sheet", "grid", "patches"}
_SHEET_KEYS = {"width", "height"}
_GRID_KEYS = {"rows", "columns"}
_PATCH_KEYS = {"row", "column", "index", "path", "offset_x", "offset_y"}


@dataclass(frozen=True)
class SheetSpec:
    width: int
    height: int


@dataclass(frozen=True)
class GridSpec:
    rows: int
    columns: int


@dataclass(frozen=True)
class PatchSpec:
    """单个补丁的静态配置（坐标已归一化为 row/column）。"""

    order: int          # 在清单中的序号（0 起），报告与叠加以此为稳定次序
    row: int
    column: int
    path: Path          # 已解析为绝对路径（相对路径基于配置文件目录）
    offset_x: float     # 原始浮点偏移，取整在叠加阶段完成
    offset_y: float


@dataclass(frozen=True)
class Config:
    sheet: SheetSpec
    grid: GridSpec
    patches: tuple[PatchSpec, ...]
    config_path: Path


def _is_int(value: Any) -> bool:
    """严格整数判定：bool 不视为整数。"""
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)))


def _require_object(value: Any, loc: str, errors: list[str]) -> dict | None:
    if not isinstance(value, dict):
        errors.append(f"{loc}: 应为 JSON 对象，实际为 {type(value).__name__}")
        return None
    return value


def _check_unknown(obj: dict, allowed: set[str], loc: str, errors: list[str]) -> None:
    for key in obj:
        if key not in allowed:
            errors.append(f"{loc}/{key}: 未知字段（允许的字段：{', '.join(sorted(allowed))}）")


def _positive_int(obj: dict, key: str, loc: str, errors: list[str]) -> int | None:
    if key not in obj:
        errors.append(f"{loc}/{key}: 缺失必填字段")
        return None
    value = obj[key]
    if not _is_int(value):
        errors.append(f"{loc}/{key}: 应为正整数，实际为 {value!r}")
        return None
    if value <= 0:
        errors.append(f"{loc}/{key}: 应为正整数，实际为 {value}")
        return None
    return int(value)


def load_config(config_path: str | Path) -> Config:
    """读取并校验配置文件，失败抛 :class:`ConfigError`。"""
    path = Path(config_path).resolve()
    errors: list[str] = []

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}: {exc}") from exc

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise ConfigError(
            f"配置文件不是合法 JSON（第 {exc.lineno} 行第 {exc.colno} 列）: {exc.msg}"
        ) from exc

    root = _require_object(data, "", errors)
    if root is None:
        raise ConfigError("配置校验失败：\n  - " + "\n  - ".join(errors))
    _check_unknown(root, _TOP_KEYS, "", errors)

    # ---- sheet ----
    sheet_w = sheet_h = None
    if "sheet" not in root:
        errors.append("/sheet: 缺失必填字段（需包含 width、height）")
    else:
        sheet_obj = _require_object(root["sheet"], "/sheet", errors)
        if sheet_obj is not None:
            _check_unknown(sheet_obj, _SHEET_KEYS, "/sheet", errors)
            sheet_w = _positive_int(sheet_obj, "width", "/sheet", errors)
            sheet_h = _positive_int(sheet_obj, "height", "/sheet", errors)

    # ---- grid ----
    grid_rows = grid_cols = None
    if "grid" not in root:
        errors.append("/grid: 缺失必填字段（需包含 rows、columns）")
    else:
        grid_obj = _require_object(root["grid"], "/grid", errors)
        if grid_obj is not None:
            _check_unknown(grid_obj, _GRID_KEYS, "/grid", errors)
            grid_rows = _positive_int(grid_obj, "rows", "/grid", errors)
            grid_cols = _positive_int(grid_obj, "columns", "/grid", errors)

    # ---- patches ----
    patch_specs: list[PatchSpec] = []
    if "patches" not in root:
        errors.append("/patches: 缺失必填字段（应为补丁清单数组，可为空数组）")
    elif not isinstance(root["patches"], list):
        errors.append(f"/patches: 应为数组，实际为 {type(root['patches']).__name__}")
    elif None not in (grid_rows, grid_cols):
        for i, item in enumerate(root["patches"]):
            loc = f"/patches/{i}"
            obj = _require_object(item, loc, errors)
            if obj is None:
                continue
            _check_unknown(obj, _PATCH_KEYS, loc, errors)

            row = obj.get("row")
            col = obj.get("column")
            index = obj.get("index")
            has_rc = "row" in obj or "column" in obj
            has_index = "index" in obj

            if has_rc and has_index:
                errors.append(f"{loc}: row/column 与 index 互斥，只能指定一种格子定位方式")
                cell_row = cell_col = None
            elif has_index:
                if not _is_int(index) or isinstance(index, bool):
                    errors.append(f"{loc}/index: 应为非负整数，实际为 {index!r}")
                    cell_row = cell_col = None
                elif index < 0 or index >= grid_rows * grid_cols:
                    errors.append(
                        f"{loc}/index: 序号 {index} 越界，合法范围为 0..{grid_rows * grid_cols - 1}"
                    )
                    cell_row = cell_col = None
                else:
                    cell_row, cell_col = divmod(int(index), grid_cols)
            elif has_rc:
                cell_row = cell_col = None
                if "row" not in obj:
                    errors.append(f"{loc}/row: 使用行列定位时缺失 row 字段")
                elif not _is_int(row) or not (0 <= int(row) < grid_rows):
                    errors.append(
                        f"{loc}/row: 应为 0..{grid_rows - 1} 之间的整数，实际为 {row!r}"
                    )
                else:
                    cell_row = int(row)
                if "column" not in obj:
                    errors.append(f"{loc}/column: 使用行列定位时缺失 column 字段")
                elif not _is_int(col) or not (0 <= int(col) < grid_cols):
                    errors.append(
                        f"{loc}/column: 应为 0..{grid_cols - 1} 之间的整数，实际为 {col!r}"
                    )
                else:
                    cell_col = int(col)
            else:
                errors.append(f"{loc}: 缺少格子定位（需提供 row+column，或 index）")
                cell_row = cell_col = None

            if "path" not in obj:
                errors.append(f"{loc}/path: 缺失必填字段（补丁图片路径）")
                patch_path = None
            elif not isinstance(obj["path"], str) or not obj["path"].strip():
                errors.append(f"{loc}/path: 应为非空字符串路径，实际为 {obj['path']!r}")
                patch_path = None
            else:
                patch_path = (path.parent / obj["path"]).resolve()

            off_x = off_y = 0.0
            for off_key in ("offset_x", "offset_y"):
                if off_key in obj and not _is_number(obj[off_key]):
                    errors.append(f"{loc}/{off_key}: 应为有限数值，实际为 {obj[off_key]!r}")
                elif off_key in obj:
                    set_off = float(obj[off_key])
                    if off_key == "offset_x":
                        off_x = set_off
                    else:
                        off_y = set_off

            if cell_row is not None and cell_col is not None and patch_path is not None:
                patch_specs.append(
                    PatchSpec(i, cell_row, cell_col, patch_path, off_x, off_y)
                )

    if errors:
        raise ConfigError("配置校验失败：\n  - " + "\n  - ".join(errors))

    return Config(
        sheet=SheetSpec(sheet_w, sheet_h),  # type: ignore[arg-type]
        grid=GridSpec(grid_rows, grid_cols),  # type: ignore[arg-type]
        patches=tuple(patch_specs),
        config_path=path,
    )
